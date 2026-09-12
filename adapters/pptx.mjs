// 复用已审查的PPT页面，按精确锚点替换文本；不重新生成无关页面。
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const [mode, jobPath] = process.argv.slice(2);
const job = JSON.parse(await fs.readFile(jobPath, 'utf8'));
const req = createRequire(path.join(job.runtime.node_modules, '_workflow_loader.cjs'));
const {PresentationFile, FileBlob} = await import(pathToFileURL(req.resolve('@oai/artifact-tool')).href);
const digest = b => crypto.createHash('sha256').update(b).digest('hex');
const sourceBytes = await fs.readFile(job.source);
if (digest(sourceBytes) !== job.source_sha256) throw new Error('基准文件指纹已变化，停止制作');
const p = await PresentationFile.importPptx(await FileBlob.load(job.source));
await fs.mkdir(job.build_dir, {recursive:true});
async function render(deck, dir) {
  await fs.mkdir(dir, {recursive:true});
  for (let i=0;i<deck.slides.items.length;i++) {
    const b=await deck.slides.items[i].export({format:'png',scale:1});
    await fs.writeFile(path.join(dir,`slide-${String(i+1).padStart(2,'0')}.png`), new Uint8Array(await b.arrayBuffer()));
  }
}
const snapshot = await p.inspect({kind:'slide,textbox,shape,image,table,chart,layout',maxChars:2000000});
await fs.writeFile(path.join(job.build_dir,'inspection.ndjson'), snapshot.ndjson);
if (mode === 'inspect') {
  await render(p,path.join(job.build_dir,'previews'));
  console.log(JSON.stringify({status:'inspected',slides:p.slides.items.length}));
} else if(mode === 'build') {
  const records=snapshot.ndjson.split('\n').filter(Boolean).map(x=>JSON.parse(x));
  const edits=[];
  for(const change of job.changes) {
    const target=records.filter(r=>r.id===change.anchor_id);
    if(target.length!==1) throw new Error(`找不到唯一编辑锚点：${change.anchor_id}`);
    if(change.old_text===change.new_text || !change.old_text) throw new Error('替换内容不能为空或没有变化');
    const shape=p.resolve(change.anchor_id);
    // 锚点来自本次检查，原文本必须匹配，防止在错误版本上静默改稿。
    const recordText=String(target[0].text??'');
    if(!recordText.includes(change.old_text)) throw new Error(`原文不匹配：${change.anchor_id}`);
    if(change.old_text.includes('\n')) {
      const oldLines=change.old_text.split('\n'), newLines=change.new_text.split('\n');
      if(oldLines.length!==newLines.length || oldLines.some(x=>!x)) throw new Error('跨段落替换须保持段落数量；重排请使用完整制作流程');
      for(let i=0;i<oldLines.length;i++) shape.text.replace(oldLines[i],newLines[i]);
    } else shape.text.replace(change.old_text,change.new_text);
    if(change.font_repair) {
      if(!job.fonts.includes(change.font_repair.typeface)) throw new Error('字体修复必须使用已核对的参考字体');
      shape.text.style=change.font_repair;
    }
    edits.push({anchor_id:change.anchor_id,slide:target[0].slide,old_text:change.old_text,new_text:change.new_text});
  }
  if(!edits.length) throw new Error('没有可执行的文本修改');
  const candidate=path.join(job.build_dir,'candidate.pptx');
  await (await PresentationFile.exportPptx(p)).save(candidate);
  const {finalizePresentation}=await import(pathToFileURL(path.join(job.runtime.skill_dir,'container_tools/artifact_tool_utils.mjs')).href);
  await finalizePresentation({workspaceDir:job.order_dir,candidatePath:candidate,finalPath:job.output,
    pythonExecutable:job.runtime.python,
    integrityValidatorPath:path.join(job.runtime.skill_dir,'container_tools/inspect_presentation_package_integrity.py'),
    layoutValidatorPath:path.join(job.runtime.skill_dir,'container_tools/inspect_presentation_layout_geometry.py'),
    layoutArgs:['--expected-slide-size-emu',job.slide_size_emu,'--validate-heading-fit'],
    explicitTotalSlideCount:job.slide_count,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],
    fontPolicy:{basis:'reference',families:job.fonts,referencePath:job.font_reference,referenceSha256:job.font_reference_sha256},
    verifyArtifactToolImport:true,receiptPath:path.join(job.build_dir,'validation.json')});
  // 检查最终导出文件，不把编辑器内存中的预览当最终文件预览。
  const final=await PresentationFile.importPptx(await FileBlob.load(job.output));
  await render(final,path.join(job.build_dir,'previews'));
  await fs.writeFile(path.join(job.build_dir,'edits.json'),JSON.stringify(edits,null,2));
  console.log(JSON.stringify({status:'built',slides:final.slides.items.length,edits:edits.length,output:job.output}));
} else throw new Error('只支持inspect或build');
