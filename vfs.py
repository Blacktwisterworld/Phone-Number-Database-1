import threading
from collections import OrderedDict
import apsw
import requests

class HTTPRangeVFS(apsw.VFS):
    def __init__(self, vfsname="httpvfs", url=None, token=None):
        self.vfsname_ = vfsname
        self.url = url
        self.token = token
        super().__init__(vfsname, base="")

    def xOpen(self, name, flags):
        return HTTPRangeFile(self.url, self.vfsname_, self.token)

class HTTPRangeFile(apsw.VFSFile):
    PAGE_SIZE = 4096
    # 12000 pages * 4KB = ~48MB RAM Cache (50GB index ke liye best)
    CACHE_MAX = 12000 

    def __init__(self, url, vfsname, token=None):
        self.url = url
        self.vfsname = vfsname
        self.token = token
        self.session = requests.Session()
        self.size = 0
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self._init_size()

    def _init_size(self):
        headers = self._headers()
        try:
            # Hugging Face se file ka size check karne ke liye GET request with Range
            headers["Range"] = "bytes=0-1"
            r = self.session.get(self.url, headers=headers, allow_redirects=True, timeout=30)
            
            if r.status_code in (200, 206):
                content_range = r.headers.get("Content-Range")
                if content_range:
                    self.size = int(content_range.split("/")[-1])
                else:
                    self.size = int(r.headers.get("Content-Length", 0))
            else:
                r = self.session.head(self.url, headers=self._headers(), allow_redirects=True, timeout=30)
                self.size = int(r.headers.get("Content-Length", 0))
        except Exception as e:
            print(f"Error getting file size: {e}")
            self.size = 0
            
        if self.size == 0:
            raise RuntimeError(f"File size 0 mila ya URL galat hai: {self.url}")

    def _headers(self):
        h = {"User-Agent": "Mozilla/5.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def xRead(self, amount, offset):
        if self.size == 0 or offset >= self.size:
            return b""
        
        end = min(offset + amount, self.size) - 1
        key = (offset, amount)
        
        # Check Cache
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
                
        headers = self._headers()
        headers["Range"] = f"bytes={offset}-{end}"
        
        try:
            r = self.session.get(self.url, headers=headers, allow_redirects=True, timeout=30)
            if r.status_code not in (200, 206):
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.content
        except Exception as e:
            raise apsw.IOError(f"Network error from {self.url}: {e}")
            
        # Save to Cache
        with self.lock:
            self.cache[key] = data
            if len(self.cache) > self.CACHE_MAX:
                self.cache.popitem(last=False) # Purana page delete karo
                
        return data

    def xWrite(self, data, offset): raise apsw.ReadOnlyError()
    def xTruncate(self, newsize): raise apsw.ReadOnlyError()
    def xSync(self, flags): return None
    def xFileSize(self): return self.size
    def xLock(self, level): return None
    def xUnlock(self, level): return None
    def xCheckReservedLock(self): return False
    def xFileControl(self, op, ptr): return False
    def xSectorSize(self): return self.PAGE_SIZE
    def xDeviceCharacteristics(self): return apsw.SQLITE_IOCAP_IMMUTABLE | apsw.SQLITE_IOCAP_ATOMIC
    def xClose(self):
        try: self.session.close()
        except: pass
