import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from astrbot_plugin_visual_media.tools.build_release import build


def project(root):
    root.mkdir()
    values={'metadata.yaml':'name: astrbot_plugin_visual_media\nversion: 0.4.0-beta.1\n',
            'main.py':'pass\n','media_settings.py':'pass\n','LICENSE':'license',
            'THIRD_PARTY_NOTICES.md':'attribution','views/workshop/index.html':'<html></html>',
            'views/workshop/studio/index.html':'<html></html>',
            'frontend/src/app.js':'export const test = true;','.astrbot-plugin/i18n/en-US.json':'{}'}
    for name,value in values.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
    return root


def test_archive_contains_rebuild_sources_and_is_reproducible(tmp_path):
    root=project(tmp_path/'project')
    (root/'validation.json').write_text('{"private":"test"}')
    (root/'.env').write_text('DO_NOT_PACKAGE=true')
    a=build(root,tmp_path/'a');b=build(root,tmp_path/'b')
    assert a['artifacts']==b['artifacts']
    for artifact in a['artifacts']:
        path=tmp_path/'a'/artifact['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==artifact['sha256']
        with zipfile.ZipFile(path) as z:
            names=z.namelist()
            assert not any(n.endswith(('validation.json','.env')) for n in names)
            assert 'astrbot_plugin_visual_media/.astrbot-plugin/i18n/en-US.json' in names
            assert ('astrbot_plugin_visual_media/frontend/src/app.js' in names)==('source.zip' in path.name)
            manifest=json.loads(z.read('astrbot_plugin_visual_media/release-manifest.json'))
            assert 'main.py' in manifest


def test_release_rejects_private_data_under_runtime_folder(tmp_path):
    root=project(tmp_path/'project');(root/'core').mkdir()
    (root/'core/private.sqlite3').write_bytes(b'test')
    with pytest.raises(ValueError,match='private'):build(root,tmp_path/'out')


def test_packaged_configuration_defaults_match_validated_settings():
    from astrbot_plugin_visual_media.media_settings import MediaSettings
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'_conf_schema.json').read_text())['media']['items']
    assert {k:v['default'] for k,v in schema.items()}==MediaSettings().public()
    assert json.loads((root/'docs/config-example.json').read_text())['media']==MediaSettings().public()
