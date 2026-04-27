from concurrent.futures import ThreadPoolExecutor
from threading import Thread

import grpc


def start_grpc_server(port: int, register_callback) -> tuple[grpc.Server, Thread]:
    server = grpc.server(ThreadPoolExecutor(max_workers=10))
    register_callback(server)
    server.add_insecure_port(f"[::]:{port}")

    def runner():
        server.start()
        server.wait_for_termination()

    thread = Thread(target=runner, daemon=True)
    thread.start()
    return server, thread
