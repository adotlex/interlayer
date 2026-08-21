import networkx as nx, igraph as ig, random, json, pathlib, re, gzip, time
from string import Template
G = nx.random_partition_graph([700,700,700,700], 0.006, 0.0004, seed=42)
g = ig.Graph.from_networkx(G)
ig.set_random_number_generator(random.Random(7))
t=time.time(); lay = g.layout("fr"); print("layout %.2fs" % (time.time()-t))
xs=[p[0] for p in lay]; ys=[p[1] for p in lay]
x0,x1,y0,y1=min(xs),max(xs),min(ys),max(ys)
nodes=[{"i":i,"x":round((p[0]-x0)/(x1-x0),4),"y":round((p[1]-y0)/(y1-y0),4),
        "n":f"Person {i}","c":i//700,"s":round(random.random()*5,2),"d":G.degree(i)} for i,p in enumerate(lay)]
edges=[[u,v] for u,v in G.edges()]
payload=json.dumps({"nodes":nodes,"edges":edges},separators=(",",":"))
TPL = Template("""<title>Interlayer network</title>
<style>:root{--bg:#fbfaf9;--fg:#191919}
:root:not([data-theme=light]) @media (prefers-color-scheme:dark){}
body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}
#c{display:block;width:100vw;height:100vh}#t{position:fixed;pointer-events:none;background:#000c;color:#fff;padding:6px 8px;border-radius:6px;font-size:12px;opacity:0}</style>
<canvas id=c></canvas><div id=t></div>
<script>const D=$payload;
const cv=document.getElementById('c'),cx=cv.getContext('2d'),tip=document.getElementById('t');
const PAL=['#3b6ea5','#c1666b','#5a9367','#b58b3c','#7d5ba6','#4a9fa8'];
let sc=1,ox=0,oy=0,W,H,hover=-1;
function fit(){W=cv.width=innerWidth*devicePixelRatio;H=cv.height=innerHeight*devicePixelRatio;draw()}
const px=n=>(n.x*Math.min(W,H)*0.9+ (W-Math.min(W,H)*0.9)/2)*sc+ox;
const py=n=>(n.y*Math.min(W,H)*0.9+ (H-Math.min(W,H)*0.9)/2)*sc+oy;
function draw(){cx.clearRect(0,0,W,H);
 cx.strokeStyle='rgba(120,120,120,.18)';cx.lineWidth=devicePixelRatio*.5;cx.beginPath();
 for(const[a,b]of D.edges){const A=D.nodes[a],B=D.nodes[b];cx.moveTo(px(A),py(A));cx.lineTo(px(B),py(B))}cx.stroke();
 for(const n of D.nodes){const r=(2+Math.sqrt(n.d))*devicePixelRatio*sc*.6;
  cx.fillStyle=PAL[n.c%PAL.length];cx.beginPath();cx.arc(px(n),py(n),r,0,6.284);cx.fill()}
 if(hover>=0){const n=D.nodes[hover];cx.strokeStyle='#191919';cx.lineWidth=2*devicePixelRatio;
  cx.beginPath();cx.arc(px(n),py(n),8*devicePixelRatio*sc,0,6.284);cx.stroke()}}
cv.addEventListener('mousemove',e=>{const mx=e.clientX*devicePixelRatio,my=e.clientY*devicePixelRatio;let best=-1,bd=1e9;
 for(const n of D.nodes){const d=(px(n)-mx)**2+(py(n)-my)**2;if(d<bd){bd=d;best=n.i}}
 if(bd<(12*devicePixelRatio)**2){hover=best;const n=D.nodes[best];
  tip.style.opacity=1;tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY+12)+'px';
  tip.textContent=n.n+' - cluster '+n.c+' - score '+n.s}else{hover=-1;tip.style.opacity=0}draw()});
let drag=null;cv.addEventListener('mousedown',e=>drag=[e.clientX,e.clientY,ox,oy]);
addEventListener('mouseup',()=>drag=null);
addEventListener('mousemove',e=>{if(drag){ox=drag[2]+(e.clientX-drag[0])*devicePixelRatio;oy=drag[3]+(e.clientY-drag[1])*devicePixelRatio;draw()}});
cv.addEventListener('wheel',e=>{e.preventDefault();const k=e.deltaY<0?1.1:1/1.1;sc*=k;draw()},{passive:false});
addEventListener('resize',fit);fit();</script>""")
h = TPL.substitute(payload=payload)
pathlib.Path("scale_canvas.html").write_text(h)
tags = re.findall(r'<(?:script|link|img|iframe)\b[^>]*>', h, re.I)
ext = [x for x in tags if re.search(r'(?:src|href)\s*=\s*["\']https?://', x, re.I)]
print(f"custom canvas @2800 nodes: {len(h)/1e3:.0f} KB  (gz {len(gzip.compress(h.encode()))/1e3:.0f} KB), external tags={len(ext)}")
print(f"  vs plotly inline: {pathlib.Path('scale_plotly.html').stat().st_size/1e6:.2f} MB  ->  {pathlib.Path('scale_plotly.html').stat().st_size/len(h):.0f}x larger")
