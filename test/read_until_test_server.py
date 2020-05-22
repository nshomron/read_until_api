"""Test grpc server for read until"""

import argparse
from collections import namedtuple
from concurrent import futures
from contextlib import closing
import logging
from queue import Queue, Empty
import socket
import sys
from threading import Thread
import time
import typing

import grpc
from minknow_api import (
    acquisition_pb2,
    acquisition_pb2_grpc,
    analysis_configuration_pb2,
    analysis_configuration_pb2_grpc,
    data_pb2,
    data_pb2_grpc,
)

LOGGER = logging.getLogger(__name__)
CLASS_MAP = {
    83: "strand",
    67: "strand1",
    77: "multiple",
    90: "zero",
    65: "adapter",
    66: "mux_uncertain",
    70: "user2",
    68: "user1",
    69: "event",
    80: "pore",
    85: "unavailable",
    84: "transition",
    78: "unclassed",
}


class AnalysisConfigurationService(
    analysis_configuration_pb2_grpc.AnalysisConfigurationServiceServicer
):
    """Test server implementation of AnalysisConfigurationService
    """

    def get_read_classifications(self, request, context):
        """Mimic get read classifications, for now return hard-coded CLASS_MAP"""
        return analysis_configuration_pb2.GetReadClassificationsResponse(
            read_classifications=CLASS_MAP,
        )


class DataService(data_pb2_grpc.DataServiceServicer):
    """
    Test server implementation of DataService.

    Contains useful methods for testing responses to get_live_reads
    """

    ChannelDataItem = namedtuple("ChannelDataItem", ["time", "data"])

    def __init__(self):
        self.live_reads_responses_to_send = Queue()
        self._live_reads_terminate = Queue()
        self.live_reads_requests = []

        self.channel_data = {}

    def add_response(self, response: data_pb2.GetLiveReadsResponse):
        """
        Add a response to be sent to any live_reads readers.

        If no readers exist, it will be send as soon as one connects.
        """
        self.live_reads_responses_to_send.put(response)

    def terminate_live_reads(self):
        """Terminate one open live reads stream."""
        self._live_reads_terminate.put(None)

    def get_data_types(self, request: data_pb2.GetDataTypesRequest, context):
        """Get the data types available from this service"""
        return data_pb2.GetDataTypesResponse(
            calibrated_signal=data_pb2.GetDataTypesResponse.DataType(
                type=data_pb2.GetDataTypesResponse.DataType.FLOATING_POINT,
                big_endian=False,
                size=4,
            ),
            uncalibrated_signal=data_pb2.GetDataTypesResponse.DataType(
                type=data_pb2.GetDataTypesResponse.DataType.SIGNED_INTEGER,
                big_endian=False,
                size=2,
            ),
        )

    def get_live_reads(self, request_iterator, _context):
        """Start streaming live reads"""

        def request_handler(self, request_iterator):
            for request in request_iterator:
                LOGGER.info("Server received request: %s", request)
                self.live_reads_requests.append(request)
                if request.HasField("actions"):
                    for action in request.actions.actions:
                        self._add_channel_data(action.channel, action)

        request_thread = Thread(target=request_handler, args=(self, request_iterator,))
        request_thread.start()

        while request_thread.is_alive():
            # If we have been asked to exit then abort
            try:
                self._live_reads_terminate.get(block=False)
                LOGGER.info("Exiting get_live_reads due to terminate request")
                return
            except Empty:
                pass

            try:
                # Send responses as the queue is filled.
                resp = self.live_reads_responses_to_send.get(timeout=0.1)
                for channel, reads in resp.channels.items():
                    self._add_channel_data(channel, reads)
                yield resp
            except Empty:
                continue

    def find_response_times(self) -> typing.List[float]:
        """Find response times (in seconds) based on sent/received data from the server"""
        response_times = []
        response_time = 0
        for _channel, data in self.channel_data.items():
            start_item = None
            for data_item in data:
                if start_item:
                    if isinstance(
                        start_item.data, data_pb2.GetLiveReadsResponse.ReadData
                    ):
                        matched = False
                        read_id = start_item.data.id
                        read_number = start_item.data.number
                        matched = (
                            read_id == data_item.data.id
                            or read_number == data_item.data.number
                        )

                        # its possible a new read comes in before the first one is responded to
                        # so we dont get match
                        if matched:
                            response_time = data_item.time - start_item.time
                            assert response_time > 0
                    response_times.append(response_time)
                    start_item = None
                else:
                    start_item = data_item

        return response_times

    def _add_channel_data(self, channel, data):
        if channel not in self.channel_data:
            self.channel_data[channel] = []
        self.channel_data[channel].append(self.ChannelDataItem(time.time(), data))


class AcquisitionService(acquisition_pb2_grpc.AcquisitionServiceServicer):
    """
    Test server implementation of AcquisitionService.
    """

    def __init__(self):
        self.progress = acquisition_pb2.GetProgressResponse()

    def get_progress(
        self, request: acquisition_pb2.GetProgressRequest, _context
    ) -> acquisition_pb2.GetProgressResponse:
        """Find current acquisition progress"""
        return self.progress


def get_free_network_port() -> int:
    """Find a free port number"""
    with closing(socket.socket()) as temp_socket:
        temp_socket.bind(("", 0))
        return temp_socket.getsockname()[1]


class ReadUntilTestServer:
    """
    Test server runs grpc read until service on a port.
    """

    def __init__(self, port=None):
        self.port = port
        if not self.port:
            self.port = get_free_network_port()
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

        self.data_service = DataService()
        data_pb2_grpc.add_DataServiceServicer_to_server(self.data_service, self.server)

        self.acquisition_service = AcquisitionService()
        acquisition_pb2_grpc.add_AcquisitionServiceServicer_to_server(
            self.acquisition_service, self.server
        )

        self.analysis_configuraion_service = AnalysisConfigurationService()
        analysis_configuration_pb2_grpc.add_AnalysisConfigurationServiceServicer_to_server(
            self.analysis_configuraion_service, self.server
        )

        LOGGER.info("Starting server. Listening on port %s.", self.port)
        self.server.add_insecure_port("[::]:%s" % self.port)
        self.server.start()

    def stop(self):
        """Stop grpc server"""
        self.server.stop(0)


def main():
    """Cli entrypoint for test server"""
    logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

    parser = argparse.ArgumentParser(description="Testing grpc read until server")
    parser.add_argument(
        "--port", default=8800, type=int, help="Port to run grpc server on"
    )
    parser.add_argument("--client", action="store_true")

    args = parser.parse_args()
    if args.client:

        def config_stream():
            while True:
                request = data_pb2.GetLiveReadsRequest(
                    setup=data_pb2.GetLiveReadsRequest.StreamSetup(
                        first_channel=1,
                        last_channel=512,
                        raw_data_type=data_pb2.GetLiveReadsRequest.CALIBRATED,
                    )
                )
                yield request
                time.sleep(60)

        LOGGER.info("Connecting to server on port %s.", args.port)
        with grpc.insecure_channel("localhost:%s" % args.port) as channel:
            try:
                grpc.channel_ready_future(channel).result(timeout=1)
            except grpc.FutureTimeoutError:
                LOGGER.info("Failed to connect to grpc")
                return

            stub = data_pb2_grpc.DataServiceStub(channel)
            for resp in stub.get_live_reads(config_stream()):
                LOGGER.info("Response: %s", resp)
                sys.stdout.flush()

    # Create a gRPC server
    server = ReadUntilTestServer(args.port)

    # Add a response for a user to receive
    server.data_service.add_response(data_pb2.GetLiveReadsResponse())

    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        server.stop()

    LOGGER.info("Server exited.")


if __name__ == "__main__":
    main()
