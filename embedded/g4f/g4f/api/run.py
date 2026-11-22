import os

import g4f.api

if __name__ == "__main__":
    # run_api() 本身只接受 port/host 参数，不读环境变量；
    # 这里补上 G4F_HOST / G4F_PORT 支持，方便用 .bat 指定端口。
    kwargs = {}
    if os.environ.get("G4F_HOST"):
        kwargs["host"] = os.environ["G4F_HOST"]
    if os.environ.get("G4F_PORT"):
        kwargs["port"] = int(os.environ["G4F_PORT"])
    g4f.api.run_api(debug=True, **kwargs)
