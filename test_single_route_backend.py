import asyncio
import json
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
USER_CONFIG_PATH = ROOT / 'user_config.json'
ORIGINAL_USER_CONFIG = USER_CONFIG_PATH.read_text(encoding='utf-8') if USER_CONFIG_PATH.exists() else None
TEST_USER_CONFIG = {
    'resume_name': 'resume.md',
    'think_model': 'qwen3:0.6b',
    'chat_model': 'qwen3:0.6b',
    'introduce': '测试用打招呼语',
    'character': '简洁 直接 礼貌',
    'tags': ['AI产品工程师', 'AI应用工程师'],
    'backend': {
        'job_score_delay_base_ms': 0,
        'job_score_delay_jitter_ms': 0,
    },
    'frontend': {
        'serverHost': 'http://127.0.0.1:8000',
        'resumeIndex': 0,
        'thread': 50,
        'timestampTimeout': 3000,
        'onlyGreet': False,
        'manualFilterWaitMs': 10000,
        'roundRestartDelayMs': 2000,
        'maxEmptyRounds': 3,
        'detailTimeout': 10000,
        'greetTimeout': 12000,
        'preloadScrollPixels': 180,
        'preloadScrollWaitMs': 450,
        'preloadStableRoundsLimit': 8,
        'preloadMaxRounds': 50,
        'preloadActivateCardEvery': 0,
        'preloadActivateCardWaitMs': 250,
    },
    'scoring': {
        'title_block_keywords': {
            '算法': 100,
            'c语言': 100,
        },
        'title_penalty_keywords': {
            '运维': 30,
            'langchain': 16,
        },
        'title_strong_keywords': {
            'ai产品工程师': 98,
            'ai应用工程师': 94,
            '智能体': 94,
            'vibe coding': 96,
        },
        'title_medium_keywords': {
            'ai': 78,
            'workflow': 74,
            'prompt': 72,
        },
        'detail_infra_keywords': {
            'claude code': 14,
            'codex': 12,
            '智能体': 10,
            '工作流': 8,
        },
        'detail_support_keywords': {
            'python': 8,
            '部署': 5,
            '代码生成': 5,
        },
        'detail_negative_keywords': {
            'langchain': 8,
            '算法': 14,
            'c语言': 16,
        },
    },
}


def install_fastapi_stub():
    if 'fastapi' in sys.modules:
        return

    fastapi_stub = types.ModuleType('fastapi')

    class FastAPI:
        def get(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

        def post(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

    class HTTPException(Exception):
        pass

    def Body(*args, **kwargs):
        return ...

    fastapi_stub.FastAPI = FastAPI
    fastapi_stub.Body = Body
    fastapi_stub.HTTPException = HTTPException
    sys.modules['fastapi'] = fastapi_stub


def purge_modules():
    for name in ['config', 'core', 'main']:
        sys.modules.pop(name, None)


class SingleRouteBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        USER_CONFIG_PATH.write_text(
            json.dumps(TEST_USER_CONFIG, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        purge_modules()

    @classmethod
    def tearDownClass(cls):
        if ORIGINAL_USER_CONFIG is None:
            USER_CONFIG_PATH.unlink(missing_ok=True)
        else:
            USER_CONFIG_PATH.write_text(ORIGINAL_USER_CONFIG, encoding='utf-8')
        purge_modules()

    def test_client_config_no_longer_exposes_profile(self):
        from config import Config

        client_config = Config.get_client_config()
        self.assertIn('introduce', client_config)
        self.assertIn('frontend', client_config)
        self.assertNotIn('profile', client_config)

    def test_single_route_delivery_uses_fixed_introduce_and_resume_index(self):
        from core import evaluateSingleRouteDelivery
        from config import Config

        job = '# 职位名称\nAI产品工程师\n\n# 薪资范围\n20-30K\n\n# 职位描述\n负责 Claude Code、Codex、智能体、工作流与代码生成调试部署'
        result = evaluateSingleRouteDelivery(job)

        self.assertEqual(result['introduce'], Config.introduce)
        self.assertEqual(result['resumeIndex'], Config.frontend.get('resumeIndex', 0))
        self.assertNotIn('profile', result)
        self.assertNotIn('route_reason', result)
        self.assertNotIn('route_scores', result)

    def test_get_job_score_returns_single_route_shape(self):
        install_fastapi_stub()
        from main import get_job_score

        job = '# 职位名称\nAI产品工程师\n\n# 薪资范围\n20-30K\n\n# 职位描述\n负责 Claude Code、Codex、智能体、工作流与代码生成调试部署'
        result = asyncio.run(get_job_score(job))

        self.assertIn('score', result)
        self.assertIn('introduce', result)
        self.assertIn('resumeIndex', result)
        self.assertNotIn('profile', result)
        self.assertNotIn('routeReason', result)
        self.assertNotIn('routeScores', result)

    def test_research_mode_collects_before_delivery_actions(self):
        script = (ROOT / 'web_script.js').read_text(encoding='utf-8')
        collection_start = script.index(
            "if (activeMode === 'research') {",
            script.index('const jobInfo = await getJobInfo(href);'),
        )
        collection_end = script.index('if (jobInfo.skip)', collection_start)
        collection_branch = script[collection_start:collection_end]

        self.assertIn('api.collectResearchJob', collection_branch)
        self.assertIn("return loop();", collection_branch)
        self.assertNotIn('api.getJobScore', collection_branch)
        self.assertNotIn('logAction(', collection_branch)
        self.assertNotIn('addToChatList(', collection_branch)
        self.assertIn("logger.getMode()", script)
        self.assertIn("logger.complete('已完成')", script)

    def test_search_settings_are_available_before_manual_start(self):
        script = (ROOT / 'web_script.js').read_text(encoding='utf-8')
        logger_start = script.index('class Logger')
        search_start = script.index('async __search(tagIdx)')
        init_start = script.index('const init = () =>', search_start)
        init_end = script.index('init();', init_start)
        init_block = script[init_start:init_end]

        self.assertIn('keywordInput', script[logger_start:search_start])
        self.assertIn('goodjobs.searchKeywords', script)
        self.assertIn('parseSearchKeywords', script)
        self.assertIn('logger.getKeywords()', script[search_start:])
        self.assertNotIn('logger.runBtn.click()', init_block)
        self.assertLess(
            script.index('ctn.appendChild(modeBox)', logger_start),
            script.index("runBtn.addEventListener('click'", logger_start),
        )

    def test_preload_round_setting_normalizes_and_persists(self):
        script = (ROOT / 'web_script.js').read_text(encoding='utf-8')
        node = shutil.which('node')
        if node is None:
            self.skipTest('node is required to execute the userscript normalization helper')

        helper_start = script.index('const DEFAULT_PRELOAD_MAX_ROUNDS')
        helper_end = script.index('// 元素选择器', helper_start)
        helper_source = script[helper_start:helper_end]
        probe = helper_source + """
console.log(JSON.stringify([
    normalizePreloadMaxRounds(''),
    normalizePreloadMaxRounds('not-a-number'),
    normalizePreloadMaxRounds('50.6'),
    normalizePreloadMaxRounds('-2'),
    normalizePreloadMaxRounds('301'),
]));
"""
        result = subprocess.run(
            [node, '-e', probe],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )

        self.assertEqual(json.loads(result.stdout), [50, 50, 51, 1, 300])
        self.assertIn("goodjobs.preloadMaxRounds", script)
        self.assertIn("preloadInput.type = 'number'", script)
        self.assertIn('preloadInput.min = String(MIN_PRELOAD_MAX_ROUNDS)', script)
        self.assertIn('preloadInput.max = String(MAX_PRELOAD_MAX_ROUNDS)', script)
        self.assertIn('this.preloadInput.disabled = true', script)
        self.assertIn("this.setMode('research')", script)

    def test_preload_flow_uses_locked_round_limit_and_job_count_only(self):
        script = (ROOT / 'web_script.js').read_text(encoding='utf-8')
        preload_start = script.index('const preloadJobs = async () =>')
        preload_end = script.index('const pickNextKeyword', preload_start)
        preload_block = script[preload_start:preload_end]
        main_start = script.index('const main = async () =>', preload_end)
        main_end = script.index('// 初始化', main_start)
        main_block = script[main_start:main_end]

        self.assertIn(
            'for (let round = 1; round <= activePreloadMaxRounds; round++)',
            preload_block,
        )
        self.assertIn('afterCount > highestCount', preload_block)
        self.assertNotIn('scrollY', preload_block)
        self.assertIn('PRELOAD_STABLE_ROUNDS_LIMIT = 8', script)
        self.assertIn('stableRounds >= OPTIONS.preloadStableRoundsLimit', preload_block)
        self.assertIn('已达到用户设置的 ${activePreloadMaxRounds} 轮上限', preload_block)
        self.assertIn('连续 ${stableRounds} 轮无新增岗位', preload_block)
        self.assertIn('开始提取职位详情', preload_block)
        self.assertLess(
            main_block.index('logger.lockSettings()'),
            main_block.index('await clientConfigPromise'),
        )
        self.assertLess(
            main_block.index('activePreloadMaxRounds = logger.getPreloadMaxRounds()'),
            main_block.index('await startRound()'),
        )

    def test_research_project_is_created_before_preloading(self):
        script = (ROOT / 'web_script.js').read_text(encoding='utf-8')
        round_start = script.index('const startRound = async () =>')
        round_end = script.index('// 主函数', round_start)
        round_block = script[round_start:round_end]

        self.assertLess(
            round_block.index('await tools.asyncSleep(OPTIONS.manualFilterWaitMs)'),
            round_block.index('await api.createResearchProject(keyword, location.href)'),
        )
        self.assertLess(
            round_block.index('await api.createResearchProject(keyword, location.href)'),
            round_block.index('await preloadJobs()'),
        )
        self.assertLess(
            round_block.index('await preloadJobs()'),
            round_block.index('loop();'),
        )

    def test_preload_defaults_are_exposed_by_backend_config(self):
        from config import Config

        self.assertEqual(Config.frontend['preloadMaxRounds'], 50)
        self.assertEqual(Config.frontend['preloadStableRoundsLimit'], 8)

    def test_client_config_does_not_override_bootstrap_server_host(self):
        script = (ROOT / 'web_script.js').read_text(encoding='utf-8')

        self.assertIn('const bootstrapServerHost = OPTIONS.serverHost', script)
        self.assertIn('OPTIONS.serverHost = bootstrapServerHost', script)


if __name__ == '__main__':
    unittest.main()
