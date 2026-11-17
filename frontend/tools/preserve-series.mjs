import {readFileSync,writeFileSync} from 'node:fs';
const file=new URL('../../views/workshop/studio/index.html',import.meta.url);
let html=readFileSync(file,'utf8');
if(!html.includes('series-panels.js'))html=html.replace('</head>',"<link rel=\"stylesheet\" href=\"../series-ui.css?v=series.3\"><script type=\"module\" src=\"../series-panels.js?v=series.3\"></script>"+'</head>');
if(!html.includes('id="dynamics-series-panel"'))html=html.replace('</header>','</header>'+"<section id=\"dynamics-series-panel\" aria-label=\"互通插件状态\"><p class=\"sp-feedback\" role=\"status\">正在读取互通插件…</p></section>");
writeFileSync(file,html);
