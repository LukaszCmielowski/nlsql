from types import SimpleNamespace
from unittest.mock import Mock

import pyarrow as pa
import pytest
from data_connect_hub import DCHConnectionError
from pyarrow import flight

from nlsql.dch import DataConnectClient, _DiscoveryFlightClient


def test_sdk_discovery_uses_deadlines_and_connection_headers(monkeypatch):
    remote = Mock()
    remote.get_flight_info.return_value = SimpleNamespace(
        endpoints=[SimpleNamespace(ticket="ticket")]
    )
    metadata = pa.table({"table_name": ["orders"]})
    remote.do_get.return_value.read_all.return_value = metadata
    monkeypatch.setattr(_DiscoveryFlightClient, "_flight_connect", lambda self: remote)
    options = Mock(wraps=flight.FlightCallOptions)
    monkeypatch.setattr(flight, "FlightCallOptions", options)

    with DataConnectClient(
        "dch.example.com:443", token="test", tenant_id="demo", flight_timeout=2.5
    ) as dch:
        assert dch.get_tables("conn", include_schema=True).equals(metadata)

    args = options.call_args.kwargs
    assert args["timeout"] == 2.5
    assert (b"x-data-connection-id", b"conn") in args["headers"]
    assert (b"authorization", b"Bearer test") in args["headers"]
    rpc_options = remote.get_flight_info.call_args.args[1]
    remote.do_get.assert_called_once_with("ticket", rpc_options)
    remote.close.assert_called_once()


def test_sdk_discovery_preserves_timeout_cause_and_closes_client(monkeypatch):
    remote = Mock()
    timeout = flight.FlightTimedOutError("test deadline")
    remote.get_flight_info.side_effect = timeout
    monkeypatch.setattr(_DiscoveryFlightClient, "_flight_connect", lambda self: remote)

    with (
        DataConnectClient("dch.example.com:443", tenant_id="demo", flight_timeout=2.5) as dch,
        pytest.raises(DCHConnectionError) as failure,
    ):
        dch.get_tables("conn", include_schema=True)

    assert failure.value.__cause__ is timeout
    remote.do_get.assert_not_called()
    remote.close.assert_called_once()
