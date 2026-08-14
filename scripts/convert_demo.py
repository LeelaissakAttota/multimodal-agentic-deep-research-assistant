from pathlib import Path
import json

inpath = Path('examples/demo_run.json')
outdir = Path('screenshots')
outdir.mkdir(exist_ok=True)
raw = inpath.read_bytes()
last = None
for enc in ('utf-8','utf-16','utf-16-le','utf-16-be'):
    try:
        s = raw.decode(enc)
        obj = json.loads(s)
        (outdir / 'demo-output.json').write_text(json.dumps(obj, indent=2), encoding='utf-8')
        print('wrote', outdir / 'demo-output.json', 'using', enc)
        break
    except Exception as e:
        last = e
else:
    print('failed to decode demo_run.json:', last)
