"""SOCKS5 authentication/remote-DNS and fail-closed checks; local fixtures only."""
import socketserver,threading,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import requests
from proxy import HTTPProxyRelay,request_proxy_context,normalize_server

seen=[]
class Handler(socketserver.BaseRequestHandler):
    def read(self,n):
        data=b''
        while len(data)<n:
            part=self.request.recv(n-len(data))
            if not part: raise OSError('closed')
            data+=part
        return data
    def handle(self):
        try:
            assert self.read(3)==bytes((5,1,2))
            self.request.sendall(bytes((5,2)))
            assert self.read(1)==b'\x01'
            user=self.read(self.read(1)[0]); password=self.read(self.read(1)[0])
            if user!=b'user' or password!=b'fixture-secret':
                self.request.sendall(b'\x01\x01');return
            self.request.sendall(b'\x01\x00')
            head=self.read(4);assert head[:3]==b'\x05\x01\x00'
            kind=head[3]
            address=self.read(self.read(1)[0] if kind==3 else 4 if kind==1 else 16)
            port=int.from_bytes(self.read(2),'big')
            seen.append((kind,address,port))
            self.request.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
            data=b''
            while not data.endswith(b'\r\n\r\n'): data+=self.read(1)
            assert b'fixture-secret' not in data and b'Proxy-Authorization' not in data
            assert data.startswith(b'GET /path?q=1 HTTP/1.1')
            self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
        except (OSError,AssertionError): return
class Server(socketserver.ThreadingTCPServer):
    daemon_threads=True
server=Server(('127.0.0.1',0),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
proxy={'server':f'socks5://127.0.0.1:{server.server_address[1]}','username':'user','password':'fixture-secret'}
try:
    for scheme in ('socks5','socks5h'):
        configured={**proxy,'server':proxy['server'].replace('socks5:',scheme+':')}
        for target,kind in [('remote.invalid',3),('192.0.2.1',1),('[2001:db8::1]',4)]:
            with request_proxy_context(configured) as opts,requests.Session() as session:
                session.trust_env=False
                r=session.get(f'http://{target}/path?q=1',timeout=5,**opts)
                assert r.status_code==200 and r.text=='OK'
            assert seen[-1][0]==kind
    with request_proxy_context({**proxy,'password':'wrong'}) as opts,requests.Session() as session:
        session.trust_env=False
        r=session.get('http://remote.invalid/path?q=1',timeout=5,**opts)
        assert r.status_code==502 and 'fixture-secret' not in r.text
    relay=HTTPProxyRelay(proxy);relay.close()
    assert not relay.thread.is_alive() and not relay._sockets
    assert normalize_server('socks5h://proxy.invalid')=='socks5h://proxy.invalid:1080'
    print('PASS: SOCKS5/SOCKS5h auth, remote DNS, IPv4/IPv6, wrong-password no-direct, no secret forwarding, cleanup')
finally:
    server.shutdown();server.server_close();thread.join(2)
