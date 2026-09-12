"""根据当前机器已提供的依赖路径建立本地配置，不安装依赖或覆盖客户材料。"""
import argparse
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('python','node','node-modules','skill-dir'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--template-database',default='reports/模板盘点_2026-09-06/template_inventory.sqlite')
    p.add_argument('--overwrite',action='store_true')
    a=p.parse_args();values=vars(a)
    target=ROOT/'config/runtime.json'
    if target.exists() and not a.overwrite:
        p.error('本机配置已存在；如确需替换，请使用 --overwrite')
    config={}
    for key in ('python','node','node_modules','skill_dir'):
        path=Path(values[key])
        if not path.is_absolute() or not path.exists():
            p.error(key+' 必须为已存在的绝对路径')
        if key in ('python','node') and not path.is_file():
            p.error(key+' 必须指向可执行文件')
        if key in ('node_modules','skill_dir') and not path.is_dir():
            p.error(key+' 必须指向目录')
        config[key]=path.resolve().as_posix()
    config['template_database']=a.template_database
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('已写入本机config/runtime.json；配置不会提交到GitHub。')

if __name__=='__main__':main()
