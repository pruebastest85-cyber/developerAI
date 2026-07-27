import urllib.request
req = urllib.request.Request('http://localhost:8080/search?q=python&format=json', headers={'User-Agent':'Mozilla/5.0','Accept':'application/json,text/html;q=0.9,*/*;q=0.8'})
with urllib.request.urlopen(req, timeout=10) as f:
    print(f.status)
    print(f.read(6000).decode('utf-8', 'ignore'))
