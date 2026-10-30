"""Loopback-only static review of real retained evidence; no scientific execution."""
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from pathlib import Path
import argparse,functools
p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=3081);a=p.parse_args()
root=Path(__file__).resolve().parent
print(f'ModAb retained-case review: http://127.0.0.1:{a.port}/review.html',flush=True)
ThreadingHTTPServer(('127.0.0.1',a.port),functools.partial(SimpleHTTPRequestHandler,directory=str(root))).serve_forever()
