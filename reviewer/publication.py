"""Revision-checked source publication using the existing Khoj store."""
import asyncio
import importlib
import sys
from pathlib import Path


class Publisher:
    def __init__(self, config):
        source = Path(config['publication']['khoj_source']).resolve()
        sys.path.insert(0, str(source))
        module = importlib.import_module('khoj_mcp.vault')
        self.error = importlib.import_module('khoj_mcp.contracts').KnowledgeError
        self.backend = module.VaultStore(
            Path(config['vault']), Path(config['publication']['state']), namespace='knowledge',
            sentinel_name='.knowledge-vault', sentinel_content=b'personal-knowledge-v1\n')

    def read(self, target):
        async def collect():
            offset, chunks, revision = 0, [], None
            while True:
                try:
                    page = await self.backend.read('knowledge/' + target, offset, 16000)
                except self.error as exc:
                    if exc.code == 'not_found' and offset == 0:
                        return None
                    raise
                if revision is not None and page['revision'] != revision:
                    raise ValueError('Destination changed during its source read')
                revision = page['revision']
                chunks.append(page['content'])
                if page['next_offset'] is None:
                    return {'content': ''.join(chunks), 'revision': revision}
                offset = page['next_offset']
        return asyncio.run(collect())

    def publish(self, snapshot):
        path = 'knowledge/' + snapshot['target']
        if snapshot['expected_revision'] is None:
            result = asyncio.run(self.backend.create(path, snapshot['note']))
        elif snapshot['before'] == '':
            result = asyncio.run(self.backend.edit(path, snapshot['expected_revision'], 'append', snapshot['note']))
        else:
            result = asyncio.run(self.backend.edit(path, snapshot['expected_revision'], 'replace',
                                                   snapshot['note'], snapshot['before']))
        saved = self.read(snapshot['target'])
        if saved is None or saved['content'] != snapshot['note'] or saved['revision'] != result['revision']:
            raise ValueError('Source verification did not match the approved text')
        return result
