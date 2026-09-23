"""检查Git暂存内容，避免将客户原件、运行数据和常见密钥误入库。"""
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PRIVATE_DIRS={'.private','orders','cache','learning','reports','node_modules','__pycache__'}
PRIVATE_SUFFIXES={'.pptx','.ppt','.docx','.pdf','.zip','.png','.jpg','.jpeg','.mp4','.sqlite','.db','.pem','.key','.log'}
PRIVATE_SUFFIXES.update({'.webp','.gif','.bmp','.tif','.tiff','.svg','.ico','.avif','.heic','.mp3','.wav','.7z','.rar'})
# 本项目内部容量预算，不是GitHub平台上限；历史对象另用git count-objects检查。
MAX_TRACKED_BYTES=20_000_000
PATTERNS=[
    ('GitHub令牌',re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})')),
    ('API密钥',re.compile(rb'\bsk-[A-Za-z0-9_-]{30,}')),
    ('私钥',re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('微信账号标识',re.compile(rb'\bwxid_[a-z0-9]{8,}')),
]

def inspect_blob(name,content):
    path=Path(name);issues=[]
    if set(path.parts)&PRIVATE_DIRS or path.suffix.lower() in PRIVATE_SUFFIXES:
        issues.append('本地资料或运行产物路径')
    if name in ('config/runtime.json','config/wechat.local.json','工作台.html') or path.name.startswith('.env') or path.name.endswith('keys.json'):
        issues.append('机器配置或凭据文件')
    if len(content)>2_000_000:issues.append('文件超过2MB，请单独核对')
    try:
        content.decode('utf-8-sig')
        if b'\x00' in content:issues.append('只允许文本，拒绝含NUL的二进制内容')
    except UnicodeDecodeError:
        issues.append('只允许UTF-8文本，二进制或其他编码须留本地')
    for title,pattern in PATTERNS:
        if pattern.search(content):issues.append(title)
    return issues

def main():
    listed=subprocess.run(['git','ls-files','-z'],cwd=ROOT,check=True,capture_output=True).stdout
    failures=[];count=0;total_bytes=0
    for encoded in listed.split(b'\0'):
        if not encoded:continue
        name=encoded.decode('utf-8');count+=1
        result=subprocess.run(['git','show',':'+name],cwd=ROOT,check=True,capture_output=True)
        total_bytes+=len(result.stdout)
        issues=inspect_blob(name,result.stdout)
        if issues:failures.append((name,issues))
    for name,issues in failures:
        print(name+'：'+'；'.join(issues))
    if total_bytes>MAX_TRACKED_BYTES:
        failures.append(('[总量]',['暂存文本总量超过项目20MB预算']))
        print('暂存文本总量超过项目20MB预算，先清理冗余文本；不自动删除历史。')
    print(f'暂存文件合计{total_bytes:,}字节；内部预算{MAX_TRACKED_BYTES:,}字节。')
    print(f'已检查Git暂存区{count}个文件；发现{len(failures)}项问题。未输出疑似密钥值。')
    return 1 if failures else 0

if __name__=='__main__':raise SystemExit(main())
