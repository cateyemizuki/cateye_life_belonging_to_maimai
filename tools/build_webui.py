"""Build the self-contained MaiBot custom page. No network/build dependencies."""
from pathlib import Path
import base64,hashlib,json,re
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'webui/source'
def build():
    template=(SRC/'index.template.html').read_text(encoding='utf-8')
    css=(SRC/'panel.css').read_text(encoding='utf-8')+'\n'+(SRC/'fish.css').read_text(encoding='utf-8')
    manifest={}
    for asset in sorted((SRC/'assets').iterdir()):
        raw=asset.read_bytes()
        mime='image/svg+xml' if asset.suffix=='.svg' else 'image/webp'
        uri='data:'+mime+';base64,'+base64.b64encode(raw).decode('ascii')
        key='FISH_URI' if asset.suffix=='.svg' else asset.stem.upper()
        css=css.replace('{{'+key+'}}',uri)
        template=template.replace('{{'+key+'}}',uri)
        if asset.suffix=='.svg':template=template.replace('{{FISH_SVG}}',raw.decode('utf-8'))
        manifest[asset.name]={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    js=(SRC/'panel.js').read_text(encoding='utf-8').replace('freshnessTimer=setInterval',(SRC/'fish.js').read_text(encoding='utf-8')+'\nfreshnessTimer=setInterval',1)
    output=template.replace('{{CSS}}',css).replace('{{JS}}',js)
    assert not re.search(r'\{\{[A-Z_]+\}\}',output),'unresolved asset slot'
    assert len(output.encode('utf-8')) < 2*1024*1024,'host page limit exceeded'
    (ROOT/'webui/index.html').write_text(output,encoding='utf-8')
    (SRC/'asset-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Built webui/index.html:',len(output.encode('utf-8')),'bytes;',len(manifest),'verified assets')
if __name__=='__main__':build()
