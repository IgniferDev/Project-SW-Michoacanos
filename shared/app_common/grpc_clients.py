from contextlib import contextmanager

import grpc

from proto_generated import academics_pb2_grpc
from proto_generated import attendance_pb2_grpc
from proto_generated import auth_pb2_grpc
from proto_generated import grades_pb2_grpc
from proto_generated import notifications_pb2_grpc
from proto_generated import periods_pb2_grpc
from proto_generated import reports_pb2_grpc


@contextmanager
def grpc_channel(target: str):
    channel = grpc.insecure_channel(target)
    try:
        yield channel
    finally:
        channel.close()


def auth_stub(target: str):
    with grpc_channel(target) as channel:
        yield auth_pb2_grpc.AuthServiceStub(channel)


def periods_stub(target: str):
    with grpc_channel(target) as channel:
        yield periods_pb2_grpc.PeriodsServiceStub(channel)


def academics_stub(target: str):
    with grpc_channel(target) as channel:
        yield academics_pb2_grpc.AcademicsServiceStub(channel)


def grades_stub(target: str):
    with grpc_channel(target) as channel:
        yield grades_pb2_grpc.GradesServiceStub(channel)


def attendance_stub(target: str):
    with grpc_channel(target) as channel:
        yield attendance_pb2_grpc.AttendanceServiceStub(channel)


def notifications_stub(target: str):
    with grpc_channel(target) as channel:
        yield notifications_pb2_grpc.NotificationsServiceStub(channel)


def reports_stub(target: str):
    with grpc_channel(target) as channel:
        yield reports_pb2_grpc.ReportsServiceStub(channel)
