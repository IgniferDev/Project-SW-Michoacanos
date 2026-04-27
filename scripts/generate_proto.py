from pathlib import Path
from grpc_tools import protoc


ROOT = Path(__file__).resolve().parents[1]
PROTO_DIR = ROOT / "proto"
OUT_DIR = ROOT / "proto_generated"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    protos = sorted(PROTO_DIR.glob("*.proto"))
    for proto in protos:
        result = protoc.main(
            [
                "protoc",
                f"-I{PROTO_DIR}",
                f"--python_out={OUT_DIR}",
                f"--grpc_python_out={OUT_DIR}",
                str(proto),
            ]
        )
        if result != 0:
            raise SystemExit(f"Failed to compile {proto.name}")
    init_file = OUT_DIR / "__init__.py"
    if not init_file.exists():
        init_file.write_text('"""Generated protobuf code."""\n', encoding="utf-8")


if __name__ == "__main__":
    main()
