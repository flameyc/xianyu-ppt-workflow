"""按指定联系人只读导出本机微信FTS消息；复用现成解密模块，不输出密钥。"""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path('E:/AI_Tools/wechat-summarizer/scripts')
KEYS = TOOLS / 'wechat_keys.json'
sys.dont_write_bytecode = True
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, 'E:/闲鱼项目/.wechat_skill_deps')


def open_memory(db, relative, keys, tmp):
    from src.wechat_reader_fts import _snapshot_and_decrypt
    con = sqlite3.connect(':memory:')
    con.deserialize(_snapshot_and_decrypt(db/relative, relative, keys, tmp))
    con.execute('PRAGMA query_only=ON')
    return con


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['search','read'])
    p.add_argument('--name')
    p.add_argument('--username')
    p.add_argument('--since', default='2026-01-01')
    p.add_argument('--until')
    p.add_argument('--output')
    a = p.parse_args()
    # 密钥仅留在当前进程，禁止写入任务包或打印异常中的变量值。
    keys = json.loads(KEYS.read_text(encoding='utf-8-sig'))
    db = Path(keys['__db_dir__']).resolve(strict=True)
    candidates = list(Path('D:/xwechat_files').glob('*/db_storage/message/message_fts.db'))
    latest = max(candidates, key=lambda x: max(x.stat().st_mtime, Path(str(x)+'-wal').stat().st_mtime if Path(str(x)+'-wal').exists() else 0))
    if latest.parent.parent.resolve() != db:
        raise ValueError('当前活动数据库与密钥文件记录的账号不一致，未读取其他账号')
    private = ROOT / '.private' / 'wechat'
    private.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='snapshot_', dir=private) as tmp_name:
        tmp = Path(tmp_name)
        with closing(open_memory(db,'contact/contact.db',keys,tmp)) as contact:
            if a.mode == 'search':
                if not a.name: raise ValueError('请提供联系人名字')
                hits=[]
                for table in ('contact','stranger'):
                    cols={x[1] for x in contact.execute(f'PRAGMA table_info([{table}])')}
                    if not {'username','nick_name','remark'} <= cols: continue
                    rows=contact.execute(f'SELECT username,nick_name,remark FROM [{table}] WHERE nick_name LIKE ? OR remark LIKE ? LIMIT 21',('%'+a.name+'%','%'+a.name+'%')).fetchall()
                    hits.extend({'username':u,'nickname':n,'remark':r} for u,n,r in rows if not str(u).endswith('@chatroom'))
                print(json.dumps({'matches':hits[:20],'limited':len(hits)>20},ensure_ascii=False))
                return
            if not a.username or not a.output: raise ValueError('读取需要唯一联系人username和本地output路径')
            matches=contact.execute('SELECT username,nick_name,remark FROM contact WHERE username=?',(a.username,)).fetchall()
            if len(matches)!=1: raise ValueError('联系人未唯一确认')
            peer={'username':matches[0][0],'nickname':matches[0][1],'remark':matches[0][2]}
        with closing(open_memory(db,'message/message_fts.db',keys,tmp)) as fts:
            mapping={str(u):int(i) for i,u in fts.execute('SELECT rowid,username FROM name2id') if u}
            sid=mapping.get(a.username)
            if sid is None: raise ValueError('联系人存在，但本地FTS未包含该会话')
            since=int(datetime.fromisoformat(a.since).timestamp())
            until=int(datetime.fromisoformat(a.until).timestamp()) if a.until else int(datetime.now().timestamp())
            names={int(i):str(u) for i,u in fts.execute('SELECT rowid,username FROM name2id') if u}
            messages=[];scanned=[];skipped=[];seen=set();limited=False
            tables=[x[0] for x in fts.execute("SELECT name FROM sqlite_master WHERE type='table'") if re.fullmatch(r'message_fts_v\d+_[A-Za-z0-9_]+',x[0])]
            for table in tables:
                cols={x[1] for x in fts.execute(f'PRAGMA table_info([{table}])')}
                required={'session_id','sender_id','create_time','acontent','message_local_id','local_type'}
                if not required<=cols: continue
                try:
                    rows=fts.execute(f'SELECT message_local_id,sender_id,create_time,acontent,local_type FROM [{table}] WHERE session_id=? AND create_time>=? AND create_time<? ORDER BY create_time LIMIT 10001',(sid,since,until)).fetchall()
                except sqlite3.Error:
                    skipped.append(table);continue
                scanned.append(table)
                if len(rows)>10000:limited=True
                for mid,sender,ct,content,kind in rows[:10000]:
                    signature=(mid,sender,ct,str(content),kind)
                    if signature in seen:continue
                    seen.add(signature)
                    role='客户' if names.get(sender)==a.username else ('我方' if sender==0 or names.get(sender,'').startswith(db.parent.name.rsplit('_',1)[0]) else '发送者待核对')
                    messages.append({'message_id':mid,'time':datetime.fromtimestamp(ct).astimezone().isoformat(),'timestamp':ct,'role':role,'content':content or '', 'local_type':kind,'table':table})
            messages.sort(key=lambda x:(x['timestamp'],x['message_id']))
            out=Path(a.output).resolve()
            if not out.is_relative_to(ROOT):raise ValueError('导出仅允许写入本工作区')
            if out.exists():raise ValueError('导出文件已存在，请使用新名称')
            out.parent.mkdir(parents=True,exist_ok=True)
            result={'exported_at':datetime.now().astimezone().isoformat(),'contact':peer,'since':a.since,'until':a.until,'source':'本机微信FTS只读快照','scope':'仅所选客户会话，FTS文本不保证含全部图片/语音/附件正文','scanned_tables':scanned,'skipped_tables':skipped,'truncated':limited,'messages':messages}
            out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'output':str(out),'contact':peer,'count':len(messages),'first':messages[0]['time'] if messages else None,'last':messages[-1]['time'] if messages else None,'skipped_tables':skipped,'truncated':limited},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        # 只给错误类型；不给出可能含敏感配置的堆栈或变量。
        print('读取未完成：'+type(exc).__name__+'；请检查账号匹配、现成密钥有效性或本机依赖。',file=sys.stderr)
        sys.exit(1)
