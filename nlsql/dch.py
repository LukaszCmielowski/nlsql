"""Apply Flight deadlines to table discovery in the pinned DCH SDK."""

from contextlib import closing

import pyarrow as pa
from data_connect_hub import DataConnectClient as SDKClient
from data_connect_hub import DCHConnectionError
from data_connect_hub._flight import FlightClient, _build_command_get_tables
from pyarrow import flight


class _DiscoveryFlightClient(FlightClient):
    def __init__(self, *, timeout: float, **kwargs):
        super().__init__(timeout=timeout, **kwargs)
        self._discovery_timeout = timeout

    def _do_get_tables(
        self,
        connection_id: str,
        *,
        table_name_filter: str | None = None,
        include_schema: bool = False,
    ) -> pa.Table:
        # The pinned SDK only applies its timeout to ADBC. Reuse its Flight
        # transport, headers, and command encoder for the two discovery RPCs.
        descriptor = flight.FlightDescriptor.for_command(
            _build_command_get_tables(
                table_name_filter_pattern=table_name_filter, include_schema=include_schema
            )
        )
        options = flight.FlightCallOptions(
            timeout=self._discovery_timeout,
            headers=[*self._flight_headers(), (b"x-data-connection-id", connection_id.encode())],
        )
        with closing(self._flight_connect()) as client:
            try:
                info = client.get_flight_info(descriptor, options)
                if not info.endpoints:
                    raise DCHConnectionError(
                        "server returned no Flight endpoints for CommandGetTables"
                    )
                return client.do_get(info.endpoints[0].ticket, options).read_all()
            except DCHConnectionError:
                raise
            except Exception as exc:
                raise DCHConnectionError(str(exc)) from exc


class DataConnectClient(SDKClient):
    def _require_flight(self) -> FlightClient:
        if self._flight is None and self._flight_kwargs["timeout"] is not None:
            self._flight = _DiscoveryFlightClient(**self._flight_kwargs)
        return super()._require_flight()
