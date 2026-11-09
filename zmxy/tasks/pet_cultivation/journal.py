"""按账号、存档和 runId 原子保存培养断点，不存登录凭据。

已放生的宠物记录不再变，追加写进旁边的 ``gpt_<runId>.released.jsonl``，主断点只留还在用的宠物，
长轮次每次存盘不用重写成千上万条历史。内存里 ``data['pets']`` 仍是全部记录；读文件用 ``load_file``。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from ... import run_records, state

RUN_ROOT = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share') / 'Zmxy5Next/pet_cultivation_runs'
# 磁盘格式，勿改：用户 AppData 里有暂停中的轮次，要能原样续跑。
PREFIX = 'gpt_pet_run:'  # 同时是任务进度里的键前缀，由它找回 runId
FILE_NAME = 'gpt_{}.json'
RELEASED_SUFFIX = run_records.PET_RELEASED_SUFFIX  # 已放生记录，每行 {"entityId":…,"record":…}；不匹配 gpt_*.json
VERSION = 1
PURPOSE = 'gpt：宠物培养可核验断点'

REPLACE_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4)
LOCKED_WINERRORS = (None, 5, 32, 33)
ARCHIVE_INTERVAL = 2


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def released_path(path):
    path = Path(path)
    return path.with_name(path.stem + RELEASED_SUFFIX)


def _read_released(path):
    """已放生记录；最后一行没写完（追加时断电）就丢掉它——那只还在主断点里。"""
    file = released_path(path)
    if not file.is_file():
        return {}
    lines = [line for line in file.read_text(encoding='utf-8').splitlines() if line.strip()]
    records = {}
    for i, line in enumerate(lines):
        try:
            row = json.loads(line)
            records[row['entityId']] = row['record']
        except (ValueError, KeyError, TypeError):
            if i == len(lines) - 1:
                break
            raise ValueError('已放生宠物记录损坏，未执行')
    return records


def load_file(path):
    """读一份培养断点：已放生记录在前，主断点里的同名记录为准。返回 (数据, 只在归档里的实体)。"""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    released = _read_released(path)
    if isinstance(data, dict) and isinstance(data.get('pets'), dict) and released:
        frozen = set(released) - set(data['pets'])
        data['pets'] = {**released, **data['pets']}
        return data, frozen
    return data, set()


def _resume_id(ctx):
    """续跑的 runId；新开返回 None。有进度却认不出 runId 时拒绝，以免把旧宠当新轮次处理。"""
    done = ctx.options.get('resume_done')
    if not done:
        return None
    runs = [k[len(PREFIX):] for k, v in done.items() if str(k).startswith(PREFIX) and state.num(v) == 1]
    if not runs:
        raise ValueError('旧培养断点没有宠物实体身份，不能安全迁移；已保留旧宠，请重新设置刷取次数启动新任务')
    if len(runs) > 1 or not re.fullmatch('[0-9a-f]{32}', runs[0]):
        raise ValueError('培养断点编号无效，未执行')
    return runs[0]


class RunJournal:
    def __init__(self, ctx, *, root=None):
        slot = ctx.session.slot or (ctx.config.record_index + 1)
        self.owner = hashlib.sha256(f'{ctx.config.username}|{ctx.config.uid}|{slot}'.encode()).hexdigest()
        resumed = _resume_id(ctx)
        self.resumed = resumed is not None
        self.run_id = resumed or uuid.uuid4().hex
        self.path = Path(root or RUN_ROOT) / self.owner / FILE_NAME.format(self.run_id)
        self.data = {}
        self.ctx = ctx
        self._last_saved = None
        self._pet_json = {}
        self._frozen = {}  # 已写进 released.jsonl 的实体 → 当时那条记录（同一个对象才算没变，主断点里不再写）
        self._archive_at = -float('inf')
        self._archive_enabled = root is None and run_records.canonical('pet', RUN_ROOT)
        self._archive_warned = False
        if self.resumed:
            self.data, frozen = load_file(self.path)
            self._frozen = {e: self.data['pets'][e] for e in frozen}
            if (not isinstance(self.data, dict) or self.data.get('owner') != self.owner
                    or self.data.get('runId') != self.run_id or self.data.get('version') != VERSION):
                raise ValueError('培养断点与当前账号/存档不匹配，未执行')
        ctx.progress[PREFIX + self.run_id] = 1

    def _payload(self):
        """长轮次里宠物记录成千上万、每步都存，只重新序列化变过的那几只。"""
        cache = {}
        for entity, record in self.data.get('pets', {}).items():
            if self._frozen.get(entity) is record:
                continue
            self._frozen.pop(entity, None)  # 记录被整条换掉了：回到主断点，以主断点为准
            old = self._pet_json.get(entity)
            cache[entity] = old if old is not None and old[0] == record else (copy.deepcopy(record), _dump(record))
        self._pet_json = cache
        pets = '{'+','.join(f'{_dump(entity)}:{text}' for entity, (_, text) in cache.items())+'}'
        return '{'+','.join(f"{_dump(k)}:{pets if k == 'pets' else _dump(v)}" for k, v in self.data.items())+'}\n'

    def _freeze_released(self):
        """新放生的记录先追加进归档文件并落盘，再从主断点里去掉；中间断了两边都有，以主断点为准。"""
        pets = self.data.get('pets')
        if not isinstance(pets, dict):
            return
        new = [e for e, r in pets.items() if self._frozen.get(e) is not r and isinstance(r, dict) and r.get('phase') == 'released']
        if not new:
            return
        file = released_path(self.path)
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open('a', encoding='utf-8') as stream:
            stream.write(''.join(_dump({'entityId': e, 'record': pets[e]}) + '\n' for e in new))
            stream.flush()
            os.fsync(stream.fileno())
        self._frozen.update((e, pets[e]) for e in new)

    def save(self):
        self._freeze_released()
        payload = self._payload()
        if payload == self._last_saved and self.path.is_file():
            self._archive()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 每次独立临时文件：替换失败时候选副本留着，下一次也不会撞上它。
        temp = self.path.with_name(f'{self.path.stem}.{uuid.uuid4().hex}.tmp')
        with temp.open('x', encoding='utf-8') as stream:
            stream.write(payload)
        for delay in (*REPLACE_DELAYS, None):
            try:
                os.replace(temp, self.path)
                break
            except PermissionError as exc:
                if delay is None or getattr(exc, 'winerror', None) not in LOCKED_WINERRORS:
                    raise
                time.sleep(delay)  # 只重试本地替换，绝不回到捕捉/回收/放生请求
        self._last_saved = payload
        self._archive()

    def _archive(self):
        if not self._archive_enabled:
            return
        final = self.data.get('status') in ('paused', 'completed')
        stable = not self.data.get('pending') and not self.data.get('field')
        if not final and (not stable or time.monotonic()-self._archive_at < ARCHIVE_INTERVAL):
            return
        try:
            if final:
                if run_records.maintain('pet', RUN_ROOT)['errors']:
                    raise OSError('部分归档未成功，对应缓存未清理')
            else:
                run_records.archive_file('pet', RUN_ROOT, self.path)
                if released_path(self.path).is_file():
                    run_records.archive_file('pet', RUN_ROOT, released_path(self.path))
            self._archive_at = time.monotonic()
        except (OSError, ValueError) as exc:
            if not self._archive_warned:
                self.ctx.log(f'实战记录归档暂未完成，本地断点保留：{exc}')
                self._archive_warned = True

    def initialize(self, **values):
        self.data = {'version': VERSION, 'purpose': PURPOSE, 'owner': self.owner, 'runId': self.run_id, **values}
        self.save()
