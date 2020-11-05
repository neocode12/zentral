import json
import logging
import os
import yaml
from zentral.core.exceptions import ImproperlyConfigured
from .config import ConfigDict, FileProxy


__all__ = ['contact_groups', 'settings', 'user_templates_dir']


logger = logging.getLogger("zentral.conf")


#
# Zentral settings
#
# JSON or YAML
# loaded from the ZENTRAL_CONF env variable or
# from the base.(json|ya?ml) file in the ZENTRAL_CONF_DIR.
#


ZENTRAL_CONF_ENV_VAR = "ZENTRAL_CONF"
ZENTRAL_CONF_DIR_ENV_VAR = "ZENTRAL_CONF_DIR"


def get_conf_dir():
    conf_dir = os.environ.get(ZENTRAL_CONF_DIR_ENV_VAR)
    if not conf_dir:
        conf_dir = os.path.realpath(os.path.join(os.path.dirname(__file__),
                                    "../../conf"))
    if os.path.exists(conf_dir):
        return conf_dir
    else:
        logger.info("Could not find configuration directory")


def get_raw_configuration():
    # env
    raw_cfg = os.environ.get(ZENTRAL_CONF_ENV_VAR)
    if raw_cfg:
        logger.info("Got raw configuration from environment")
        return raw_cfg, "ENV"

    # file
    conf_dir = get_conf_dir()
    if conf_dir:
        for filename in ("base.json", "base.yaml", "base.yml"):
            filepath = os.path.join(conf_dir, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r") as f:
                        raw_cfg = f.read()
                except Exception:
                    logger.exception("Could not read configuration file {}".format(filepath))
                else:
                    logger.info("Got raw configuration from file {}".format(filepath))
                    return raw_cfg, filepath

    raise ImproperlyConfigured("Could not get raw configuration")


def get_configuration():
    raw_cfg, src = get_raw_configuration()
    for loader, kwargs in ((json.loads, {}),
                           (yaml.load, {"Loader": yaml.SafeLoader})):
        try:
            return loader(raw_cfg, **kwargs)
        except (ValueError, yaml.YAMLError):
            pass
    raise ImproperlyConfigured("Could not parse raw configuration from {}".format(src))


class APIDict(ConfigDict):
    """Special ConfigDict class for the "api" section

    Get values from deprecated configuration keys.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # load deprecated tls_server_certs
        tls_server_certs = self.get("tls_server_certs")
        if tls_server_certs and "tls_fullchain" not in self:
            logger.warning("Loading tls_fullchain from deprecated tls_server_certs")
            self._collection["tls_fullchain"] = FileProxy(tls_server_certs)
        # load deprecated tls_server_key
        tls_server_key = self.get("tls_server_key")
        if tls_server_key and "tls_privkey" not in self:
            logger.warning("Loading tls_privkey from deprecated tls_server_key")
            self._collection["tls_privkey"] = FileProxy(tls_server_key)


class ZentralSettings(ConfigDict):
    custom_classes = {
        # use special config class for the api dict
        ("api",): APIDict
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # add default apps
        for app in ["zentral.core.incidents", "zentral.core.probes"]:
            self.setdefault("apps", {})[app] = {}


settings = ZentralSettings(get_configuration())


#
# User templates
#
# The user can override the default templates by putting
# correctly named templates in the templates/ directory
# of the configuration directory
#


def get_user_templates_dir():
    conf_dir = get_conf_dir()
    if conf_dir:
        template_dir = os.path.join(conf_dir, "templates")
        if os.path.isdir(template_dir):
            return template_dir


user_templates_dir = get_user_templates_dir()


#
# Contact groups
#
# TODO: deprecated, replace with something better
#


contact_groups = {}
