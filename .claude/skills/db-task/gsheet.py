"""Minimal Sheets helper (stdlib + openssl, no pip needed). Usage:
  gsheet.py KEY SHEET_ID meta
  gsheet.py KEY SHEET_ID get  'Tab!A1:Z50'
  gsheet.py KEY SHEET_ID append TAB RESULT_CSV [--dry-run]
  gsheet.py KEY SHEET_ID set 'Tab!A5' VALUE      (overwrite one cell)

append: add rows of a db-task result.csv to the end of TAB, matching columns by the
sheet's header names. PROCESS_DATE = the task folder name (parent dir of RESULT_CSV),
who = "auto". A row is skipped if the sheet already has the same PROCESS_DATE + id +
process + is_success, so re-running is safe.
"""
import sys, os, csv, json, time, base64, subprocess, urllib.request, urllib.parse
SCOPE = 'https://www.googleapis.com/auth/spreadsheets'
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b'=')
def token(keyfile):
    k = json.load(open(keyfile)); now = int(time.time())
    hdr = b64(json.dumps({'alg':'RS256','typ':'JWT'}).encode())
    claim = b64(json.dumps({'iss':k['client_email'],'scope':SCOPE,'aud':k['token_uri'],'iat':now,'exp':now+3000}).encode())
    msg = hdr+b'.'+claim
    import tempfile, os
    with tempfile.NamedTemporaryFile('w', delete=False) as f:
        f.write(k['private_key']); pk = f.name
    os.chmod(pk, 0o600)
    try: sig = subprocess.run(['openssl','dgst','-sha256','-sign',pk], input=msg, capture_output=True, check=True).stdout
    finally: os.unlink(pk)
    body = urllib.parse.urlencode({'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer','assertion':(msg+b'.'+b64(sig)).decode()}).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(k['token_uri'], body)))['access_token']
def call(tok, url, data=None, method=None):
    r = urllib.request.Request(url, headers={'Authorization':'Bearer '+tok}, **({'data':json.dumps(data).encode(),'method':method or 'POST'} if data else {}))
    if data: r.add_header('Content-Type','application/json')
    try: return json.load(urllib.request.urlopen(r))
    except urllib.error.HTTPError as e: sys.exit(f'HTTP {e.code}: {e.read().decode()[:400]}')
if __name__ == '__main__':
    key, sid, cmd = sys.argv[1:4]; tok = token(key)
    base = f'https://sheets.googleapis.com/v4/spreadsheets/{sid}'
    if cmd == 'meta':
        m = call(tok, base+'?fields=properties.title,sheets.properties(title,gridProperties)')
        print(m['properties']['title']); [print(' -', s['properties']['title'], s['properties']['gridProperties']) for s in m['sheets']]
    elif cmd == 'append':
        tab, path = sys.argv[4:6]; dry = '--dry-run' in sys.argv
        q = urllib.parse.quote(tab)
        sheet = call(tok, f'{base}/values/{q}').get('values', [])
        header = sheet[0]; col = {h: i for i, h in enumerate(header)}
        def cell(r, h): return r[col[h]] if h in col and col[h] < len(r) else ''
        date = os.path.basename(os.path.dirname(os.path.abspath(path)))
        seen = {(cell(r, 'PROCESS_DATE'), cell(r, 'id'), cell(r, 'process'), cell(r, 'is_success').upper()) for r in sheet[1:]}
        out = []
        for r in csv.DictReader(open(path, encoding='utf-8-sig')):
            r['is_success'] = r['is_success'].upper()
            if (date, r['id'], r['process'], r['is_success']) in seen:
                print('skip (already in sheet):', r['id'], r['folder_name'], r['process']); continue
            r.update(PROCESS_DATE=date, who='auto')
            out.append([r.get(h, '') for h in header])
            print('append:', r['id'], r['folder_name'], r['process'], r['is_success'])
        if out and not dry:
            call(tok, f'{base}/values/{q}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS', {'values': out})
            print(f'appended {len(out)} row(s)')
        elif dry: print(f'[dry-run] {len(out)} row(s) would be appended')
    elif cmd == 'set':
        rng, val = sys.argv[4:6]
        call(tok, f'{base}/values/{urllib.parse.quote(rng)}?valueInputOption=USER_ENTERED', {'values': [[val]]}, 'PUT')
        print('set', rng, '=', val)
    elif cmd == 'get':
        print(json.dumps(call(tok, base+'/values/'+urllib.parse.quote(sys.argv[4]))['values'], ensure_ascii=False))
