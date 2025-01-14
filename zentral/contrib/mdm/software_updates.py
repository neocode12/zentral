import datetime
import logging
from django.db import transaction
import requests
from .crypto import IPHONE_DEVICE_CA_FULLCHAIN
from .models import SoftwareUpdate, SoftwareUpdateDeviceID


logger = logging.getLogger("zentral.contrib.mdm.software_updates")


def _fetch_software_updates():
    r = requests.get("https://gdmf.apple.com/v2/pmv", verify=IPHONE_DEVICE_CA_FULLCHAIN)
    r.raise_for_status()
    return r.json()


def _parse_date(date):
    return datetime.datetime.strptime(date, "%Y-%m-%d").date()


def _iter_software_updates(response):
    for attr, public in (("PublicAssetSets", True), ("AssetSets", False)):
        products = response.get(attr)
        if not products:
            continue
        for platform, product_info_list in products.items():
            for product_info in product_info_list:
                # kwargs
                kwargs = {
                    "platform": platform,
                    "public": public,
                }
                posting_date = _parse_date(product_info["PostingDate"])
                expiration_date = None
                raw_expiration_date = product_info.get("ExpirationDate")
                if raw_expiration_date:
                    expiration_date = _parse_date(raw_expiration_date)
                kwargs["availability"] = (posting_date, expiration_date)
                kwargs.update(dict(zip(("major", "minor", "patch"),
                                       (int(s) for s in product_info["ProductVersion"].split(".")))))
                if kwargs.get("patch") is None:
                    kwargs["patch"] = 0
                # defaults
                yield kwargs, product_info["SupportedDevices"]


def sync_software_updates():
    response = _fetch_software_updates()
    with transaction.atomic():
        seen_software_updates = []
        for kwargs, supported_devices in _iter_software_updates(response):
            su, _ = SoftwareUpdate.objects.update_or_create(**kwargs)
            seen_software_updates.append(su.pk)
            for device_id in supported_devices:
                sd, _ = SoftwareUpdateDeviceID.objects.get_or_create(software_update=su, device_id=device_id)
            (SoftwareUpdateDeviceID.objects.filter(software_update=su)
                                           .exclude(device_id__in=supported_devices).delete())
        SoftwareUpdate.objects.exclude(pk__in=seen_software_updates).delete()


def available_software_updates(enrolled_device, date=None):
    major_update = minor_update = patch_update = None
    current_comparable_os_version = enrolled_device.comparable_os_version
    if current_comparable_os_version == (0, 0, 0):
        logger.debug("Enrolled device %s: no comparable OS version", enrolled_device.udid)
        return major_update, minor_update, patch_update
    current_major, current_minor, current_patch = current_comparable_os_version
    try:
        device_id = enrolled_device.device_information["SoftwareUpdateDeviceID"]
    except (KeyError, TypeError):
        logger.debug("Enrolled device %s: no SoftwareUpdateDeviceID found", enrolled_device.udid)
        return major_update, minor_update, patch_update
    if not isinstance(device_id, str):
        # should never happen
        logger.error("Enrolled device %s: SoftwareUpdateDeviceID is not a str", enrolled_device.udid)
        return major_update, minor_update, patch_update
    if not device_id:
        # should never happen
        logger.error("Enrolled device %s: SoftwareUpdateDeviceID is an empty str", enrolled_device.udid)
        return major_update, minor_update, patch_update
    # filter the choices
    if date is None:
        date = datetime.date.today()
    for software_update in SoftwareUpdate.objects.filter(
        public=False,
        availability__contains=date,
        softwareupdatedeviceid__device_id=device_id
    ):
        if software_update.comparable_os_version <= current_comparable_os_version:
            # not an update for the device
            continue
        if software_update.major != current_major:
            if major_update is None or (major_update.comparable_os_version < software_update.comparable_os_version):
                major_update = software_update
        elif software_update.minor != current_minor:
            if minor_update is None or (minor_update.comparable_os_version < software_update.comparable_os_version):
                minor_update = software_update
        elif software_update.patch != current_patch:
            if patch_update is None or (patch_update.comparable_os_version < software_update.comparable_os_version):
                patch_update = software_update
    return major_update, minor_update, patch_update


def iter_available_software_updates(enrolled_device, date=None):
    for software_update in available_software_updates(enrolled_device, date):
        if software_update:
            yield software_update
