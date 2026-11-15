"""Reviewed OpenCode 1.18.23 launch configuration and dedicated login state."""
import json
import os
from pathlib import Path
import re


VERSION = '1.18.23'
EXECUTABLE_SHA256 = 'f831518278ded5090c41cc532b16ab80629e980f710a0b46d1e5b605808bb1d9'
LOGIN_KEYS = ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','XDG_CONFIG_HOME','XDG_DATA_HOME',
              'XDG_CACHE_HOME','XDG_STATE_HOME','OPENCODE_TEST_HOME','OPENCODE_TEST_MANAGED_CONFIG_DIR',
              'OPENCODE_DISABLE_PROJECT_CONFIG','OPENCODE_PURE','OPENCODE_DISABLE_DEFAULT_PLUGINS')


def environment(workspace, home, model=None, allocation=500, login=False):
    workspace, home = Path(workspace), Path(home)
    workspace.mkdir(parents=True, exist_ok=True)
    state = home/'login-state' if login else workspace/'opencode-state'
    env = {key:os.environ[key] for key in ('SystemRoot','WINDIR','COMSPEC') if key in os.environ}
    for key in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_CACHE_HOME','XDG_STATE_HOME'):
        folder = home/'data' if key == 'XDG_DATA_HOME' else state/key.lower()
        folder.mkdir(parents=True, exist_ok=True)
        env[key] = str(folder)
    scratch = workspace/'runtime-temp'; scratch.mkdir(exist_ok=True)
    env.update(TEMP=str(scratch), TMP=str(scratch), OPENCODE_TEST_HOME=env['HOME'],
               OPENCODE_TEST_MANAGED_CONFIG_DIR=str(state/'empty-managed'),
               PATH=os.environ.get('SystemRoot','C:\\Windows')+'\\System32')
    for flag in ('PURE','DISABLE_DEFAULT_PLUGINS','DISABLE_EXTERNAL_SKILLS','DISABLE_LSP_DOWNLOAD',
                 'DISABLE_CLAUDE_CODE','DISABLE_MODELS_FETCH','DISABLE_AUTOUPDATE','DISABLE_PROJECT_CONFIG',
                 'DISABLE_AUTOCOMPACT','DISABLE_PRUNE','EXPERIMENTAL_DISABLE_FILEWATCHER'):
        env['OPENCODE_'+flag] = 'true'
    env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] = str(allocation)
    # OpenCode checks this unused plugin dependency even with all plugins off.
    # Satisfy its metadata check, without installing or executing any package.
    config = Path(env['XDG_CONFIG_HOME'])/'opencode'
    (config/'node_modules').mkdir(parents=True, exist_ok=True)
    (config/'package.json').write_text('{}', encoding='utf-8')
    (config/'package-lock.json').write_text(json.dumps({'packages':{'':{'dependencies':{'@opencode-ai/plugin':VERSION}}}}), encoding='utf-8')
    settings = dict(permission={'*':'deny'}, plugin=[], mcp={}, instructions=[], share='disabled', autoupdate=False,
        lsp=False, formatter=False, agent={name:{'disable':True} for name in ('title','summary','compaction','explore','general')})
    if model:
        if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+/[A-Za-z0-9_./:-]+',model):
            raise ValueError('Expected provider/model')
        settings.update(model=model,small_model=model,enabled_providers=[model.split('/',1)[0]])
    else:
        settings['enabled_providers'] = []
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(settings)
    env.update(npm_config_registry='http://127.0.0.1:1',npm_config_offline='true')
    return env


def authentication_from_list(output, model):
    """Only API credential labels from the dedicated CLI state count as configured."""
    clean = re.sub(r'\x1b\[[0-9;]*m','',output)
    names = {'openai':'OpenAI','anthropic':'Anthropic','google':'Google','opencode':'OpenCode Zen',
             'openrouter':'OpenRouter'}
    provider = (model or '').split('/',1)[0]
    name = names.get(provider)
    if name and re.search(r'(?m)(?:^|[│┌└●■\s])'+re.escape(name)+r'\s+api\s*$', clean):
        return 'authenticated'
    return 'unauthenticated'
