"""Private worker protocol; the startup gate precedes third-party/candidate code."""
import hashlib
import json
from pathlib import Path
import sys
import types
import warnings


def main():
    folder = Path(sys.argv[1])
    if sys.stdin.buffer.read(1) != b"G":
        return 2
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    stage = "startup"
    status, payload = "load_error", None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            import pandas as pd
        frame = pd.DataFrame(request["records"])
        stage = "load"
        (folder / "stage").write_text(stage, encoding="ascii")
        with Path(request["candidate"]).open("rb") as source:
            raw = source.read(request["limits"]["candidate_bytes"] + 1)
        if (len(raw) > request["limits"]["candidate_bytes"] or
                hashlib.sha256(raw).hexdigest() != request["sha256"]):
            status = "digest_mismatch"
        else:
            module = types.ModuleType("synthetic_candidate")
            module.__file__ = request["candidate"]
            exec(compile(raw, request["candidate"], "exec"), module.__dict__)
            analyze = getattr(module, "analyze")
            if not callable(analyze):
                raise TypeError()
            stage = "analyze"
            (folder / "stage").write_text(stage, encoding="ascii")
            payload = analyze(frame)
            stage = "normalize"
            (folder / "stage").write_text(stage, encoding="ascii")
            status = "success"
    except SystemExit:
        status, payload = "system_exit", None
    except BaseException:
        status = {"startup": "load_error", "load": "load_error",
                  "analyze": "analyze_error", "normalize": "normalization_error"}[stage]
        payload = None
    try:
        count = 0
        with (folder / "result.json").open("wb") as output:
            for chunk in json.JSONEncoder(allow_nan=False).iterencode({"status": status, "payload": payload}):
                data = chunk.encode("utf-8")
                count += len(data)
                if count > request["limits"]["result_bytes"]:
                    raise OverflowError()
                output.write(data)
    except BaseException as error:
        status = "result_limit" if isinstance(error, OverflowError) else "normalization_error"
        (folder / "result.json").write_text(json.dumps({"status": status, "payload": None}), encoding="utf-8")
    if status == "success":
        (folder / "stage").write_text("complete", encoding="ascii")
    return 0


if __name__ == "__main__":
    sys.exit(main())
