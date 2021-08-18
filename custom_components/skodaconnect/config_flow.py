import homeassistant.helpers.config_validation as cv
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_RESOURCES,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from skodaconnect import Connection
from . import get_convert_conf
from .const import (
    CONF_CONVERT,
    CONF_DEBUG,
    CONVERT_DICT,
    CONF_MUTABLE,
    CONF_UPDATE_INTERVAL,
    CONF_SPIN,
    CONF_VEHICLE,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    DEFAULT_DEBUG
)

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = {
    vol.Required(CONF_USERNAME, default=""): str,
    vol.Required(CONF_PASSWORD, default=""): str,
    vol.Optional(CONF_UPDATE_INTERVAL, default=1): cv.positive_int,
    vol.Optional(CONF_MUTABLE, default=True): cv.boolean,
    vol.Optional(CONF_CONVERT, default=None): vol.In(CONVERT_DICT),
}


class SkodaConnectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    task_login = None
    task_finish = None
    entry = None

    def __init__(self):
        """Initialize."""
        self._entry = None
        self._init_info = {}
        self._errors = {}
        self._connection = None
        self._session = None

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self.task_login = None
            self.task_update = None
            self.task_finish = None
            self._errors = {}
            self._init_info = user_input

            _LOGGER.debug("Creating connection to Skoda Connect")
            self._connection = Connection(
                session=async_get_clientsession(self.hass),
                username=self._init_info[CONF_USERNAME],
                password=self._init_info[CONF_PASSWORD],
                fulldebug=self._init_info.get(CONF_DEBUG, DEFAULT_DEBUG),
            )

            return await self.async_step_login()

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(DATA_SCHEMA), errors=self._errors
        )

    # noinspection PyBroadException
    async def _async_task_login(self):
        try:
            await self._connection.doLogin()
        except Exception as e:
            _LOGGER.error("Failed to login due to error: %s", str(e))
            self._errors["base"] = "cannot_connect"

        if not self._connection.logged_in:
            self._errors["base"] = "cannot_connect"

        self.hass.async_create_task(
            self.hass.config_entries.flow.async_configure(flow_id=self.flow_id)
        )

    async def async_step_select_vehicle(self, user_input=None):
        if user_input is not None:
            self._init_info[CONF_VEHICLE] = user_input[CONF_VEHICLE]

            return await self.async_step_set_spin()

        vin_numbers = self._init_info["CONF_VEHICLES"].keys()
        return self.async_show_form(
            step_id="select_vehicle",
            errors=self._errors,
            data_schema=vol.Schema({vol.Required(CONF_VEHICLE): vol.In(vin_numbers)}),
        )

    async def async_step_set_spin(self, user_input=None):
        if user_input is not None:
            self._init_info[CONF_SPIN] = user_input[CONF_SPIN]
            #del self._init_info["CONF_SPIN"]

            return await self.async_step_select_instruments()

        return self.async_show_form(
            step_id="set_spin",
            errors=self._errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SPIN, default=""): cv.string
                }
            ),
        )

    async def async_step_select_instruments(self, user_input=None):
        if user_input is not None:
            self._init_info[CONF_RESOURCES] = user_input[CONF_RESOURCES]
            del self._init_info["CONF_VEHICLES"]

            await self.async_set_unique_id(self._init_info[CONF_VEHICLE])
            self._abort_if_unique_id_configured()

            _LOGGER.info(f"Creating entry with config {self._init_info}")
            return self.async_create_entry(
                title=self._init_info[CONF_VEHICLE], data=self._init_info
            )

        instruments = self._init_info["CONF_VEHICLES"][self._init_info[CONF_VEHICLE]]
        instruments_dict = {
            instrument.attr: instrument.name for instrument in instruments
        }
        return self.async_show_form(
            step_id="select_instruments",
            errors=self._errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_RESOURCES, default=list(instruments_dict.keys())
                    ): cv.multi_select(instruments_dict)
                }
            ),
        )

    async def async_step_login(self, user_input=None):
        if not self.task_login:
            self.task_login = self.hass.async_create_task(self._async_task_login())

            return self.async_show_progress(
                step_id="login",
                progress_action="task_login",
            )

        # noinspection PyBroadException
        try:
            await self.task_login
        except Exception:
            return self.async_abort(reason="Failed to connect to Skoda Connect")

        if self._errors:
            return self.async_show_progress_done(next_step_id="user")

        for vehicle in self._connection.vehicles:
            _LOGGER.info(f"Found data for VIN: {vehicle.vin} from Skoda Connect")

        self._init_info["CONF_VEHICLES"] = {
            vehicle.vin: vehicle.dashboard().instruments
            for vehicle in self._connection.vehicles
        }

        return self.async_show_progress_done(next_step_id="select_vehicle")

    async def async_step_reauth(self, entry) -> dict:
        """Handle initiation of re-authentication with Skoda Connect."""
        self.entry = entry
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict = None) -> dict:
        """Handle re-authentication with Skoda Connect."""
        errors: dict = {}

        if user_input is not None:
            _LOGGER.debug("Creating connection to Skoda Connect")
            self._connection = Connection(
                session=async_get_clientsession(self.hass),
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                fulldebug=self.entry.options.get(CONF_DEBUG, self.entry.data.get(CONF_DEBUG, DEFAULT_DEBUG)),
            )

            # noinspection PyBroadException
            try:
                await self._connection.doLogin()

                if not await self._connection.validate_login:
                    _LOGGER.debug("Unable to login to Skoda Connect. Need to accept a new EULA/T&C? Try logging in to the portal: https://www.skoda-connect.com/")
                    errors["base"] = "cannot_connect"
                else:
                    data = self.entry.data.copy()
                    self.hass.config_entries.async_update_entry(
                        self.entry,
                        data={
                            **data,
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                        },
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(self.entry.entry_id)
                    )

                    return self.async_abort(reason="reauth_successful")
            except Exception as e:
                _LOGGER.error("Failed to login due to error: %s", str(e))
                return self.async_abort(reason="Failed to connect to Connect")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=self.entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return SkodaConnectOptionsFlowHandler(config_entry)


class SkodaConnectOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Plaato options."""

    def __init__(self, config_entry: ConfigEntry):
        """Initialize domain options flow."""
        super().__init__()

        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Backward compatibility
        default_convert_conf = get_convert_conf(self._config_entry)
        _LOGGER.info(f"Config entry options: {self._config_entry.options}")
        _LOGGER.info(f"Config entry data: {self._config_entry.data}")
        _LOGGER.info(f"Config entry yaml: {default_convert_conf}")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_UPDATE_INTERVAL,
                        default=self._config_entry.options.get(
                            CONF_UPDATE_INTERVAL, self._config_entry.data.get(
                                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
                        )
                    ): cv.positive_int,
                    vol.Optional(
                        CONF_SPIN,
                        self._config_entry.options.get(
                            CONF_SPIN, self._config_entry.data.get(
                                CONF_SPIN, "")
                        )
                    ): cv.string,
                    vol.Optional(
                        CONF_DEBUG,
                        default=self._config_entry.options.get(
                            CONF_DEBUG, self._config_entry.data.get(
                                CONF_DEBUG, DEFAULT_DEBUG
                            )
                        )
                    ): cv.boolean,
                    vol.Optional(
                        CONF_MUTABLE,
                        default=self._config_entry.options.get(
                            CONF_MUTABLE, self._config_entry.data.get(
                                CONF_MUTABLE, True
                            )
                        )
                    ): cv.boolean,
                    vol.Optional(
                        CONF_CONVERT,
                        default=self._config_entry.options.get(
                            CONF_CONVERT, self._config_entry.data.get(
                                CONF_CONVERT, default_convert_conf)
                        )
                    ): vol.In(CONVERT_DICT),
                }
            ),
        )
