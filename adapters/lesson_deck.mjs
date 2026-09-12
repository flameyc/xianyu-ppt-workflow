// 教学内容与版式分离：两条路线复用同一份已核对页面文案。
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const [orderDir, route, runId='20260912-v1']=process.argv.slice(2);
if(!['economy','quality'].includes(route)) throw Error('未知制作路线');
const root=path.resolve(orderDir,'../..');
const runtime=JSON.parse(await fs.readFile(path.join(root,'config/runtime.json'),'utf8'));
process.env.RUNTIME_NODE_MODULES=runtime.node_modules;
const req=createRequire(path.join(runtime.node_modules,'_loader.cjs'));
const {Presentation,PresentationFile,FileBlob}=await import(pathToFileURL(req.resolve('@oai/artifact-tool')).href);
const {finalizePresentation}=await import(pathToFileURL(path.join(runtime.skill_dir,'container_tools/artifact_tool_utils.mjs')).href);
const content=JSON.parse(await fs.readFile(path.join(orderDir,'plans/content.json'),'utf8'));
const build=path.join(orderDir,'build',runId+'-'+route); await fs.mkdir(build,{recursive:true});
const p=Presentation.create({slideSize:{width:1280,height:720}});
const quality=route==='quality';
const color=quality?{bg:'#F8F5EC',ink:'#183B35',accent:'#A54D35',muted:'#586961'}:{bg:'#FFFFFF',ink:'#23374D',accent:'#236D93',muted:'#5D6975'};
function txt(s,text,x,y,w,h,size=28,bold=false,ink=color.ink){
 const sh=s.shapes.add({geometry:'textbox',position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
 sh.text=text;sh.text.style={typeface:'微软雅黑',fontSize:size,bold,color:ink,autoFit:'none'};
 return sh;
}
for(const d of content.slides){
 const s=p.slides.add();s.background.fill=color.bg;
 const n=d.number;
 // 原稿全文保存在备注中，正文结构化重排；引文可核性另在导读页明确。
 s.speakerNotes.textFrame.setText('来源：现存《AI三分钟，考试一场空，修改版.pptx》第'+n+'页。导读和策略部分另据《修改.docx》。\n\n原稿文字：\n'+d.source_text);
 if(d.kind==='cover'){
  if(quality){
   txt(s,d.subtitle,68,105,600,50,27,false,color.muted);
   txt(s,d.title,68,223,624,210,66,true);
   txt(s,'当我想一键搜答案时，\n如何找回思考主动权',68,510,590,120,29,false,color.muted);
   s.images.add({blob:new Uint8Array(await fs.readFile(path.join(orderDir,'assets/独立思考插画.png'))),contentType:'image/png',alt:'学生独立思考，手机扣在桌边的主题插画',fit:'contain',position:{left:737,top:42,width:475,height:635}});
   s.speakerNotes.textFrame.setText('主题插画为AI生成的虚构情境。\n'+d.source_text);
   continue;
  }
  s.background.fill=quality?'#183B35':'#23374D';
  txt(s,d.subtitle,72,85,1100,50,26,false,'#D9DFD9');
  txt(s,d.title,72,200,1136,220,quality?76:68,true,'#FFFFFF');
  txt(s,d.groups[0].body,72,515,1110,90,30,false,'#E2E8E4');
  continue;
 }
 const titleSize=d.title.length>23?36:42;
 txt(s,d.title,64,42,1150,80,titleSize,true);
 if(d.subtitle && !(quality&&d.kind==='closing'))txt(s,d.subtitle,68,123,1120,55,24,false,color.muted);
 const top=d.subtitle?205:172;
 if(d.kind==='dense'){
  d.groups.forEach((g,i)=>{let x=66+i*390;txt(s,g.heading,x,184,364,44,28,true,color.accent);txt(s,g.body,x,240,359,382,19.5);});
  txt(s,'引文按客户材料保留；2025年调研比例及政策表述待核实。',68,644,1140,34,18,false,color.muted);
 }else if(quality && d.kind==='columns'){
  d.groups.forEach((g,i)=>{const x=68+i*391;txt(s,g.heading,x,top+5,352,82,g.heading.length<4?58:32,true,color.accent);txt(s,g.body,x,top+125,342,290,30);});
 }else if(quality && d.kind==='hero'){
  const g=d.groups[0];txt(s,g.heading,68,top+20,475,115,62,true,color.accent);txt(s,g.body,68,top+150,465,210,30);
  d.groups.slice(1).forEach((g,i)=>{txt(s,g.heading,642,top+i*180,558,50,32,true);txt(s,g.body,642,top+60+i*180,552,145,29);});
 }else if(quality && d.kind==='closing'){
  txt(s,d.subtitle,68,190,1130,150,52,true,color.accent);
  d.groups.forEach((g,i)=>{txt(s,[g.heading,g.body].filter(Boolean).join('\n'),68,390+i*128,1120,115,29);});
 }else if(quality && d.kind==='contrast' && d.groups.length===2 && n!==3){
  d.groups.forEach((g,i)=>{const x=68+i*602;txt(s,g.heading,x,top,540,60,36,true,color.accent);const body=g.body.replace(/[①②③]\s*/g,'').replaceAll('\n','\n\n').replace('题目读三遍，圈出已知条件，写下未知量。','题目读三遍，圈出已知条件，\n写下未知量。');txt(s,body,x,top+85,524,350,28);});
 }else{
  const available=quality?465:475;const step=available/d.groups.length;
  d.groups.forEach((g,i)=>{
   const y=top+i*step;const heading=g.heading.replace('：','\n');txt(s,heading,68,y,quality?240:220,Math.min(step-12,100),heading.length>9?25:29,true,color.accent);
   txt(s,g.body,quality?330:310,y,quality?874:894,step-16,g.body.length>120?24:28);
  });
 }
 txt(s,String(n).padStart(2,'0'),1180,666,50,30,16,false,color.muted);
}
const candidate=path.join(build,'candidate.pptx');
const outputDir=path.join(orderDir,'artifacts',runId);await fs.mkdir(outputDir,{recursive:true});
const output=path.join(outputDir,route==='economy'?'省Token路线_22页.pptx':'质量路线_22页.pptx');
await(await PresentationFile.exportPptx(p)).save(candidate);
await finalizePresentation({workspaceDir:orderDir,candidatePath:candidate,finalPath:output,pythonExecutable:runtime.python,
 integrityValidatorPath:path.join(runtime.skill_dir,'container_tools/inspect_presentation_package_integrity.py'),
 layoutValidatorPath:path.join(runtime.skill_dir,'container_tools/inspect_presentation_layout_geometry.py'),
 layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-heading-fit'],explicitTotalSlideCount:22,
 requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:['微软雅黑']},
 verifyArtifactToolImport:true,receiptPath:path.join(build,'validation.json')});
const final=await PresentationFile.importPptx(await FileBlob.load(output));
await fs.mkdir(path.join(build,'previews'),{recursive:true});
for(let i=0;i<final.slides.items.length;i++){
 const blob=await final.slides.items[i].export({format:'png',scale:1});
 await fs.writeFile(path.join(build,'previews',String(i+1).padStart(2,'0')+'.png'),new Uint8Array(await blob.arrayBuffer()));
}
console.log(JSON.stringify({route,output,slides:22}));
