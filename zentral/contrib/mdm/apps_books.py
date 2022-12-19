from datetime import datetime, timedelta
from itertools import islice
import logging
import threading
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils.functional import SimpleLazyObject
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util import Retry
from urllib.parse import urljoin
from base.utils import deployment_info
from zentral.conf import settings
from zentral.core.events.base import EventMetadata
from .commands.install_application import InstallApplication
from .events import (AssetCreatedEvent, AssetUpdatedEvent,
                     DeviceAssignmentCreatedEvent, DeviceAssignmentDeletedEvent,
                     ServerTokenAssetCreatedEvent, ServerTokenAssetUpdatedEvent)
from .incidents import MDMAssetAvailabilityIncident
from .models import (Asset, ArtifactType, ArtifactVersion, DeviceAssignment,
                     EnrolledDeviceAssetAssociation,
                     ServerToken, ServerTokenAsset)


logger = logging.getLogger("zentral.contrib.mdm.apps_books")


# API client


class CustomHTTPAdapter(HTTPAdapter):
    def __init__(self, default_timeout, retries):
        self.default_timeout = default_timeout
        super().__init__(
            max_retries=Retry(
                total=retries + 1,
                backoff_factor=1,
                status_forcelist=[500, 502, 503, 504]
            )
        )

    def send(self, *args, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None:
            kwargs["timeout"] = self.default_timeout
        return super().send(*args, **kwargs)


class AppsBooksAPIError(Exception):
    pass


class MDMConflictError(Exception):
    pass


class FetchedDataUpdatedError(Exception):
    pass


class AppsBooksClient:
    base_url = "https://vpp.itunes.apple.com/mdm/v2/"
    timeout = 5
    retries = 2

    def __init__(
        self,
        token=None,
        mdm_info_id=None,
        location_name=None,
        platform=None,
        server_token=None
    ):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": deployment_info.user_agent,
            "Authorization": "Bearer " + token
        })
        adapter = CustomHTTPAdapter(self.timeout, self.retries)
        self.session.mount("https://", adapter)
        self.mdm_info_id = mdm_info_id
        self.location_name = location_name
        self.platform = platform or "enterprisestore"
        self._service_config = None
        self.server_token = server_token

    @classmethod
    def from_server_token(cls, server_token):
        return cls(server_token.get_token(),
                   str(server_token.mdm_info_id),
                   server_token.location_name,
                   server_token.platform,
                   server_token)

    def close(self):
        self.session.close()

    def make_request(self, path, retry_if_invalid_token=True, verify_mdm_info=False, **kwargs):
        url = urljoin(self.base_url, path)
        if "json" in kwargs:
            method = self.session.post
        else:
            method = self.session.get
        resp = method(url, **kwargs)
        resp.raise_for_status()
        response = resp.json()
        errorNumber = response.get("errorNumber")
        if errorNumber:
            if errorNumber == 9622 and retry_if_invalid_token:
                if not self.server_token:
                    raise AppsBooksAPIError("Invalid token")
                logger.debug("Location %s: refresh session token", self.location_name)
                self.server_token.refresh_from_db()
                self.token = self.server_token.get_token()
                self.session.headers["Authorization"] = "Bearer " + self.token
                return self.make_request(path, False, verify_mdm_info, **kwargs)
            else:
                logger.error("Location %s: API error %s %s",
                             self.location_name, errorNumber, response.get("errorMessage", "-"))
                raise AppsBooksAPIError(f"Error {errorNumber}")
        if (
            verify_mdm_info
            and self.mdm_info_id is not None
            and response.get("mdmInfo", {}).get("id") != self.mdm_info_id
        ):
            msg = f"Location {self.location_name}: mdmInfo mismatch"
            logger.error(msg)
            raise MDMConflictError(msg)
        return response

    # client config

    def get_client_config(self):
        return self.make_request("client/config", verify_mdm_info=True)

    def update_client_config(self, notification_auth_token):
        assert self.mdm_info_id is not None and notification_auth_token is not None
        return self.make_request(
            "client/config",
            json={
                "mdmInfo": {
                    "id": self.mdm_info_id,
                    "metadata": settings["api"]["fqdn"],
                    "name": "Zentral"
                },
                "notificationTypes": ["ASSET_MANAGEMENT", "ASSET_COUNT"],
                "notificationUrl": "https://{}{}".format(
                    settings["api"]["webhook_fqdn"],
                    reverse("mdm:notify_server_token", args=(self.mdm_info_id,))
                ),
                "notificationAuthToken": notification_auth_token,
            }
        )

    # service config

    def get_service_config(self):
        if not self._service_config:
            self._service_config = self.make_request("service/config")
        return self._service_config

    # assets

    def get_asset(self, adam_id, pricing_param):
        response = self.make_request("assets", params={"adamId": adam_id, "pricingParam": pricing_param})
        try:
            return response["assets"][0]
        except IndexError:
            pass

    def iter_assets(self):
        current_version_id = None
        current_page = 0
        while True:
            logger.debug("Location %s: fetch asset page %s", self.location_name, current_page)
            response = self.make_request("assets", params={"pageIndex": current_page})
            version_id = response["versionId"]
            if current_version_id is None:
                current_version_id = version_id
            elif current_version_id != version_id:
                logger.error("Writes occured to the assets while iterating over them")
                raise FetchedDataUpdatedError
            for asset in response.get("assets", []):
                yield asset
            try:
                next_page = int(response["nextPageIndex"])
            except KeyError:
                logger.debug("Location %s: last asset page", self.location_name)
                break
            else:
                if next_page != current_page + 1:
                    logger.error("Location %s: nextPageIndex != current page + 1", self.location_name)
                    # should never happen
                    raise ValueError
                current_page = next_page

    def get_asset_metadata(self, adam_id):
        service_config = self.get_service_config()
        url = service_config.get("urls", {}).get("contentMetadataLookup")
        if not url:
            logger.error("Location %s: missing or empty contentMetadataLookup", self.location_name)
            return
        try:
            resp = requests.get(
                url,
                params={"version": 2,
                        "p": "mdm-lockup",  # TODO: Really?
                        "caller": "MDM",
                        "platform": self.platform,
                        "cc": "us",
                        "l": "en",
                        "id": adam_id},
                cookies={"itvt": self.token}
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("Location %s: could not get asset %s metadata.", self.location_name, adam_id)
        else:
            return resp.json().get("results", {}).get(adam_id)

    # assignments

    def iter_asset_device_assignments(self, adam_id, pricing_param):
        current_version_id = None
        current_page = 0
        while True:
            logger.debug("Location %s: fetch assignment page %s", self.location_name, current_page)
            response = self.make_request("assignments", params={"adamId": adam_id, "pageIndex": current_page})
            version_id = response["versionId"]
            if current_version_id is None:
                current_version_id = version_id
            elif current_version_id != version_id:
                logger.error("Writes occured to the assignments while iterating over them")
                raise FetchedDataUpdatedError
            for asset in response.get("assignments", []):
                if asset["pricingParam"] != pricing_param:
                    continue
                serial_number = asset.get("serialNumber")
                if not serial_number:
                    logger.error("Location %s: asset %s/%s with user assignments",
                                 self.location_name, adam_id, pricing_param)
                else:
                    yield serial_number
            try:
                next_page = int(response["nextPageIndex"])
            except KeyError:
                logger.debug("Location %s: last assignment page", self.location_name)
                break
            else:
                if next_page != current_page + 1:
                    logger.error("Location %s: nextPageIndex != current page + 1", self.location_name)
                    # should never happen
                    raise ValueError
                current_page = next_page

    def post_device_association(self, serial_number, asset):
        return self.make_request(
            "assets/associate",
            json={
                "assets": [{
                    "adamId": asset.adam_id,
                    "pricingParam": asset.pricing_param,
                }],
                "serialNumbers": [serial_number]
            },
        )

    def post_device_disassociation(self, serial_number, asset):
        return self.make_request(
            "assets/disassociate",
            json={
                "assets": [{
                    "adamId": asset.adam_id,
                    "pricingParam": asset.pricing_param,
                }],
                "serialNumbers": [serial_number]
            },
        )


# server token cache


class ServerTokenCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._server_tokens = {}

    def get(self, mdm_info_id):
        if not isinstance(mdm_info_id, str):
            mdm_info_id = str(mdm_info_id)
        with self._lock:
            try:
                return self._server_tokens[mdm_info_id]
            except KeyError:
                server_token = None
                client = None
                try:
                    server_token = ServerToken.objects.get(mdm_info_id=mdm_info_id)
                except ServerToken.DoesNotExist:
                    raise KeyError
                else:
                    client = AppsBooksClient.from_server_token(server_token)
                self._server_tokens[mdm_info_id] = server_token, client
                return server_token, client


server_token_cache = SimpleLazyObject(lambda: ServerTokenCache())


#
# on-the-fly assignment
#
# Instead of sending the InstallApplication command directly a device asset
# association is triggered and an EnrolledDeviceAssetAssociation object is
# created. When the assignment notification is received, the
# EnrolledDeviceAssetAssociation is retrieved to check if there is an artifact
# version to install.  The EnrolledDeviceAssetAssociation object is also used
# to avoid triggering the association too often.
#


def ensure_enrolled_device_asset_association(enrolled_device, asset):
    server_token = enrolled_device.server_token
    serial_number = enrolled_device.serial_number
    if not server_token:
        logger.error("enrolled device %s: no server token", serial_number)
        return False
    if DeviceAssignment.objects.filter(
        serial_number=serial_number,
        server_token_asset__asset=asset,
        server_token_asset__server_token=server_token
    ).count():
        logger.error("enrolled device %s: no server token", serial_number)
        return True
    with transaction.atomic():
        edaa, created = EnrolledDeviceAssetAssociation.objects.select_for_update().get_or_create(
            enrolled_device=enrolled_device,
            asset=asset
        )
        if created or (datetime.utcnow() - edaa.last_attempted_at) > timedelta(minutes=30):  # TODO hardcoded, verify
            _, client = server_token_cache.get(server_token.mdm_info_id)
            ok = False
            try:
                response = client.post_device_association(serial_number, asset)
            except Exception:
                logger.exception("enrolled device %s asset %s/%s/%s: could not post association",
                                 serial_number, server_token.location_name, asset.adam_id, asset.pricing_param)
            else:
                event_id = response.get("eventId")
                if event_id:
                    ok = True
            if not ok:
                edaa.delete()
            else:
                edaa.attempts = F("attempts") + 1
                edaa.last_attempted_at = datetime.utcnow()
                edaa.save()
    return False


def queue_install_application_command_if_necessary(server_token, serial_number, adam_id, pricing_param):
    with transaction.atomic():
        try:
            edaa = EnrolledDeviceAssetAssociation.objects.select_for_update().select_related(
                "enrolled_device",
                "asset"
            ).get(
                enrolled_device__serial_number=serial_number,
                enrolled_device__server_token=server_token,
                asset__adam_id=adam_id,
                asset__pricing_param=pricing_param
            )
        except EnrolledDeviceAssetAssociation.DoesNotExist:
            logger.error("enrolled device %s asset %s/%s/%s: no awaiting association found",
                         serial_number, server_token.location_name, adam_id, pricing_param)
        else:
            enrolled_device = edaa.enrolled_device
            # find the latest artifact version to install for this asset
            for artifact_version in ArtifactVersion.objects.next_to_install(enrolled_device, fetch_all=True):
                if (
                    artifact_version.artifact.type == ArtifactType.StoreApp.name
                    and artifact_version.store_app.asset == edaa.asset
                ):
                    InstallApplication.create_for_device(
                        enrolled_device, artifact_version, queue=True
                    )
                    break
            else:
                logger.error("enrolled device %s asset %s/%s/%s: no artifact version to install found",
                             serial_number, server_token.location_name, adam_id, pricing_param)
            # cleanup
            edaa.delete()


def clear_on_the_fly_assignment(server_token, serial_number, adam_id, pricing_param, reason):
    count, _ = EnrolledDeviceAssetAssociation.objects.filter(
        enrolled_device__serial_number=serial_number,
        enrolled_device__server_token=server_token,
        asset__adam_id=adam_id,
        asset__pricing_param=pricing_param
    ).delete()
    if count:
        logger.error("enrolled device %s asset %s/%s/%s: on-the-fly assignment canceled, %s",
                     serial_number, server_token.location_name, adam_id, pricing_param, reason)


# assets & assignments sync


def _update_or_create_asset(adam_id, pricing_param, defaults, notification_id, collected_objects):
    asset, created = Asset.objects.select_for_update().get_or_create(
        adam_id=adam_id,
        pricing_param=pricing_param,
        defaults=defaults
    )
    collected_objects["asset"] = asset
    if created:
        payload = asset.serialize_for_event(keys_only=False)
        if notification_id:
            payload["notification_id"] = notification_id
        yield AssetCreatedEvent(EventMetadata(), payload)
    else:
        updated = False
        for attr, new_val in defaults.items():
            old_val = getattr(asset, attr)
            if old_val != new_val:
                setattr(asset, attr, new_val)
                updated = True
        if updated:
            asset.save()
            payload = asset.serialize_for_event(keys_only=False)
            if notification_id:
                payload["notification_id"] = notification_id
            yield AssetUpdatedEvent(EventMetadata(), payload)


def _get_server_token_asset_event_metadata(server_token_asset):
    incident_updates = []
    incident_update_severity = server_token_asset.get_availability_incident_severity()
    if incident_update_severity is not None:
        incident_updates.append(
            MDMAssetAvailabilityIncident.build_incident_update(
                server_token_asset, incident_update_severity
            )
        )
    return EventMetadata(incident_updates=incident_updates)


def _update_or_create_server_token_asset(server_token, defaults, notification_id, collected_objects):
    asset = collected_objects["asset"]
    server_token_asset, created = ServerTokenAsset.objects.select_for_update().get_or_create(
        server_token=server_token,
        asset=asset,
        defaults=defaults
    )
    collected_objects["server_token_asset"] = server_token_asset
    if created:
        payload = server_token_asset.serialize_for_event(
            keys_only=False, server_token=server_token, asset=asset
        )
        if notification_id:
            payload["notification_id"] = notification_id
        yield ServerTokenAssetCreatedEvent(
            _get_server_token_asset_event_metadata(server_token_asset),
            payload
        )
    else:
        updated = False
        for attr, new_val in defaults.items():
            old_val = getattr(server_token_asset, attr)
            if old_val != new_val:
                setattr(server_token_asset, attr, new_val)
                updated = True
        if updated:
            server_token_asset.save()
            payload = server_token_asset.serialize_for_event(
                    keys_only=False, server_token=server_token, asset=asset
            )
            if notification_id:
                payload["notification_id"] = notification_id
            yield ServerTokenAssetUpdatedEvent(
                _get_server_token_asset_event_metadata(server_token_asset),
                payload
            )


def _update_assignments(server_token, all_serial_numbers, notification_id, collected_objects):
    asset = collected_objects["asset"]
    server_token_asset = collected_objects["server_token_asset"]
    existing_serial_numbers = set(server_token_asset.deviceassignment_set.values_list("serial_number", flat=True))
    if all_serial_numbers == existing_serial_numbers:
        return

    # prepare common event payload
    payload = server_token_asset.serialize_for_event(keys_only=False, server_token=server_token, asset=asset)
    if notification_id:
        payload["notification_id"] = notification_id

    # prune assignments
    removed_serial_numbers = existing_serial_numbers - all_serial_numbers
    if removed_serial_numbers:
        DeviceAssignment.objects.filter(server_token_asset=server_token_asset,
                                        serial_number__in=removed_serial_numbers).delete()
        for serial_number in removed_serial_numbers:
            yield DeviceAssignmentDeletedEvent(EventMetadata(machine_serial_number=serial_number), payload)

    # add missing assignments
    added_serial_numbers = all_serial_numbers - existing_serial_numbers
    if not added_serial_numbers:
        return
    batch_size = 1000  # TODO: hard-coded
    assignments_to_create = (DeviceAssignment(server_token_asset=server_token_asset,
                                              serial_number=serial_number)
                             for serial_number in added_serial_numbers)
    while True:
        batch = list(islice(assignments_to_create, batch_size))
        if not batch:
            break
        DeviceAssignment.objects.bulk_create(batch, batch_size)
    for serial_number in added_serial_numbers:
        yield DeviceAssignmentCreatedEvent(EventMetadata(machine_serial_number=serial_number), payload)


def _sync_asset_d(server_token, client, asset_d, notification_id=None):
    adam_id = asset_d["adamId"]
    pricing_param = asset_d["pricingParam"]

    asset_defaults = {
        "product_type": Asset.ProductType(asset_d["productType"]),
        "device_assignable": asset_d["deviceAssignable"],
        "revocable": asset_d["revocable"],
        "supported_platforms": asset_d["supportedPlatforms"],
    }
    metadata = client.get_asset_metadata(adam_id)
    if metadata:
        asset_defaults["metadata"] = metadata
        asset_defaults["name"] = metadata.get("name")
        asset_defaults["bundle_id"] = metadata.get("bundleId")

    server_token_asset_defaults = {
        "assigned_count": asset_d["assignedCount"],
        "available_count": asset_d["availableCount"],
        "retired_count": asset_d["retiredCount"],
        "total_count": asset_d["totalCount"],
    }

    all_serial_numbers = set(client.iter_asset_device_assignments(adam_id, pricing_param))

    with transaction.atomic():
        collected_objects = {}

        # asset
        yield from _update_or_create_asset(
            adam_id, pricing_param,
            asset_defaults,
            notification_id,
            collected_objects
        )

        # server token asset
        yield from _update_or_create_server_token_asset(
            server_token,
            server_token_asset_defaults,
            notification_id,
            collected_objects
        )

        # device assignments
        yield from _update_assignments(
            server_token, all_serial_numbers,
            notification_id,
            collected_objects
        )


def sync_asset(server_token, client, adam_id, pricing_param, notification_id):
    asset_d = client.get_asset(adam_id, pricing_param)
    if not asset_d:
        logger.error("Unknown asset %s/%s", adam_id, pricing_param)
        return
    yield from _sync_asset_d(server_token, client, asset_d, notification_id)


def sync_assets(server_token):
    client = AppsBooksClient.from_server_token(server_token)
    for asset_d in client.iter_assets():
        for event in _sync_asset_d(server_token, client, asset_d):
            event.post()


def _update_server_token_asset_counts(server_token_asset, updates, notification_id):
    updated = False
    for attr, count_delta in updates.items():
        if count_delta != 0:
            updated = True
        setattr(server_token_asset, attr, getattr(server_token_asset, attr) + count_delta)
    if not updated:
        return
    if server_token_asset.count_errors():
        raise ValueError
    else:
        server_token_asset.save()
        event_payload = server_token_asset.serialize_for_event(keys_only=False)
        event_payload["notification_id"] = notification_id
        yield ServerTokenAssetUpdatedEvent(
            _get_server_token_asset_event_metadata(server_token_asset),
            event_payload
        )


def update_server_token_asset_counts(server_token, client, adam_id, pricing_param, updates, notification_id):
    logger.debug("location %s asset %s/%s: update counts",
                 server_token.location_name, adam_id, pricing_param)
    with transaction.atomic():
        try:
            server_token_asset = (
                server_token.servertokenasset_set
                            .select_for_update()
                            .select_related("asset", "server_token")
                            .get(asset__adam_id=adam_id,
                                 asset__pricing_param=pricing_param)
            )
        except ServerTokenAsset.DoesNotExist:
            logger.info("location %s asset %s/%s: unknown, could not update counts, sync required",
                        server_token.location_name, adam_id, pricing_param)
        else:
            try:
                yield from _update_server_token_asset_counts(server_token_asset, updates, notification_id)
            except ValueError:
                logger.info("location %s asset %s/%s: %s, sync required",
                            server_token.location_name, adam_id, pricing_param,
                            ", ".join(server_token_asset.count_errors()))
            else:
                return
    yield from sync_asset(server_token, client, adam_id, pricing_param, notification_id)


def associate_server_token_asset(
    server_token, client,
    adam_id, pricing_param, serial_numbers,
    event_id, notification_id
):
    with transaction.atomic():
        try:
            server_token_asset = (
                ServerTokenAsset.objects
                                .select_for_update()
                                .select_related("asset", "server_token")
                                .get(server_token=server_token,
                                     asset__adam_id=adam_id,
                                     asset__pricing_param=pricing_param)
            )
        except ServerTokenAsset.DoesNotExist:
            logger.error("location %s asset %s/%s: unknown asset, cannot associate, sync required",
                         server_token.location_name, adam_id, pricing_param)
            yield from sync_asset(server_token, client, adam_id, pricing_param, notification_id)
        else:
            payload = server_token_asset.serialize_for_event(server_token=server_token)
            if event_id:
                payload["event_id"] = event_id
            if notification_id:
                payload["notification_id"] = notification_id
            assigned_count_delta = 0
            for serial_number in serial_numbers:
                _, created = DeviceAssignment.objects.get_or_create(
                    server_token_asset=server_token_asset,
                    serial_number=serial_number
                )
                if created:
                    assigned_count_delta += 1
                    yield DeviceAssignmentCreatedEvent(
                        EventMetadata(machine_serial_number=serial_number),
                        payload
                    )
                    # on-the-fly asset assignment done?
                    queue_install_application_command_if_necessary(
                        server_token, serial_number, adam_id, pricing_param
                    )
            try:
                yield from _update_server_token_asset_counts(
                    server_token_asset,
                    {"assigned_count": assigned_count_delta,
                     "available_count": -1 * assigned_count_delta},
                    notification_id
                )
            except ValueError:
                logger.error("location %s asset %s/%s: bad assigned count after associations, sync required",
                             server_token.location_name, adam_id, pricing_param)
                yield from sync_asset(server_token, client, adam_id, pricing_param, notification_id)


def disassociate_server_token_asset(
    server_token, client,
    adam_id, pricing_param, serial_numbers,
    event_id, notification_id
):
    with transaction.atomic():
        try:
            server_token_asset = (
                ServerTokenAsset.objects
                                .select_for_update()
                                .select_related("asset", "server_token")
                                .get(server_token=server_token,
                                     asset__adam_id=adam_id,
                                     asset__pricing_param=pricing_param)
            )
        except ServerTokenAsset.DoesNotExist:
            logger.error("location %s asset %s/%s: unknown asset, cannot disassociate, sync required",
                         server_token.location_name, adam_id, pricing_param)
            yield from sync_asset(server_token, client, adam_id, pricing_param, notification_id)
        else:
            payload = server_token_asset.serialize_for_event(server_token=server_token)
            if event_id:
                payload["event_id"] = event_id
            if notification_id:
                payload["notification_id"] = notification_id
            assigned_count_delta = 0
            for serial_number in serial_numbers:
                deleted = DeviceAssignment.objects.filter(
                    server_token_asset=server_token_asset,
                    serial_number=serial_number
                ).delete()
                if deleted:
                    assigned_count_delta -= 1
                    yield DeviceAssignmentDeletedEvent(
                        EventMetadata(machine_serial_number=serial_number),
                        payload
                    )
                # disassociated, remove the on-the-fly assignment if it exists
                clear_on_the_fly_assignment(
                    server_token, serial_number, adam_id, pricing_param, "disassociate success"
                )
            try:
                yield from _update_server_token_asset_counts(
                    server_token_asset,
                    {"assigned_count": assigned_count_delta,
                     "available_count": -1 * assigned_count_delta},
                    notification_id
                )
            except ValueError:
                logger.error("location %s asset %s/%s: bad assigned count after disassociations, sync required",
                             server_token.location_name, adam_id, pricing_param)
                yield from sync_asset(server_token, client, adam_id, pricing_param, notification_id)