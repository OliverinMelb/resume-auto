// ==UserScript==
// @name         goodJobs
// @namespace    http://tampermonkey.net/
// @version      2026.07.28.4
// @description  goodJobs篡改猴插件
// @match        https://www.zhipin.com/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=zhipin.com
// @grant        GM_xmlhttpRequest
// ==/UserScript==

(function () {
    'use strict';

    // 配置项
    const OPTIONS = {
        resumeIndex: 0, // 第几份简历，从 0 开始递增
        serverHost: 'http://127.0.0.1:8010', // 本地服务的主机地址
        thread: 50, // 分数阈值，低于这个就不发消息了
        timestampTimeout: 3000, // 时间戳过期时间，单位毫秒，根据当前网络设定，建议不要太大。
        onlyGreet: false, // 是否只打招呼，默认为false，即打招呼和代聊天
        manualFilterWaitMs: 10000, // 每轮搜索后留给用户手动筛选的时间
        roundRestartDelayMs: 2000, // 本轮结束后，启动下一轮前的缓冲时间
        maxEmptyRounds: 3, // 连续多少轮没有拿到新岗位后停止，避免空转
        detailTimeout: 10000, // 获取职位详情超时时间
        greetTimeout: 12000, // 打招呼页回执超时时间
        preloadScrollPixels: 180, // 岗位预加载：每轮下滑像素
        preloadScrollWaitMs: 450, // 岗位预加载：每轮等待毫秒数
        preloadStableRoundsLimit: 8, // 岗位预加载：连续多少轮无增长后结束
        preloadMaxRounds: 50, // 岗位预加载：每个关键词最多滑动多少轮
        preloadActivateCardEvery: 0, // 预加载时每隔多少轮尝试轻点一次左侧岗位卡片，0 表示关闭
        preloadActivateCardWaitMs: 250, // 轻点岗位卡片后的额外等待时间
    };

    const SCRIPT_VERSION = '2026.07.28.4';
    const SEARCH_KEYWORDS_STORAGE_KEY = 'goodjobs.searchKeywords';
    const PRELOAD_MAX_ROUNDS_STORAGE_KEY = 'goodjobs.preloadMaxRounds';
    const DEFAULT_PRELOAD_MAX_ROUNDS = 50;
    const MIN_PRELOAD_MAX_ROUNDS = 1;
    const MAX_PRELOAD_MAX_ROUNDS = 300;
    const PRELOAD_STABLE_ROUNDS_LIMIT = 8;
    const FALLBACK_SEARCH_KEYWORDS = [
        'AI产品工程师',
        'AI应用工程师',
        'AI产品经理',
        'AI Agent',
        '智能体',
        '工作流工程师',
        '大模型应用',
        '提示词工程师',
    ];

    function parseSearchKeywords(value) {
        const values = Array.isArray(value) ? value : String(value || '').split(/[\n,，;；]+/);
        return Array.from(new Set(values.map(item => String(item).trim()).filter(Boolean)));
    }

    function normalizePreloadMaxRounds(value, fallback = DEFAULT_PRELOAD_MAX_ROUNDS) {
        const fallbackNumber = Number(fallback);
        const normalizedFallback = Number.isFinite(fallbackNumber)
            ? Math.min(MAX_PRELOAD_MAX_ROUNDS, Math.max(MIN_PRELOAD_MAX_ROUNDS, Math.round(fallbackNumber)))
            : DEFAULT_PRELOAD_MAX_ROUNDS;
        if (value === null || value === undefined || String(value).trim() === '') {
            return normalizedFallback;
        }
        const numericValue = Number(value);
        if (!Number.isFinite(numericValue)) {
            return normalizedFallback;
        }
        return Math.min(MAX_PRELOAD_MAX_ROUNDS, Math.max(MIN_PRELOAD_MAX_ROUNDS, Math.round(numericValue)));
    }

    function safeQuery(root, selectors) {
        if (!root || typeof root.querySelector !== 'function') return null;
        const selectorList = Array.isArray(selectors) ? selectors : [selectors];
        for (const selector of selectorList) {
            if (typeof selector !== 'string' || !selector.trim()) continue;
            try {
                const element = root.querySelector(selector);
                if (element) return element;
            } catch (e) {
                // Boss 页面结构可能临时变化；当前选择器不可用时尝试下一个。
            }
        }
        return null;
    }

    function safeQueryAll(root, selector) {
        if (!root || typeof root.querySelectorAll !== 'function') return [];
        try {
            return Array.from(root.querySelectorAll(selector));
        } catch (e) {
            return [];
        }
    }

    function safeElementText(element) {
        if (!element) return '';
        try {
            return String(element.innerText || element.textContent || '').trim();
        } catch (e) {
            return '';
        }
    }

    function safeText(root, selectors) {
        return safeElementText(safeQuery(root, selectors));
    }

    function safeAttribute(element, name) {
        if (!element || typeof element.getAttribute !== 'function') return '';
        try {
            return String(element.getAttribute(name) || '').trim();
        } catch (e) {
            return '';
        }
    }

    // 元素选择器
    const SELECTORS = {
        ZHIPIN: {
            SEARCH: {
                SEARCHINPUT: 'input', // 搜索框
                SEARCHBTN: '.search-btn', // 搜索按钮
                JOBLISTCTN: '.job-list-container', // 职位列表容器
                JOBLIST: '.rec-job-list', // 职位列表
                JOBCARD: '.job-card-box', // 左侧岗位卡片
                JOBHREFS: '.job-card-box .job-name', // 职位链接
                COMPANY: '.company-name', // 搜索卡片公司名称
            },
            DETAIL: {
                STARTCHAT: '.btn-startchat', // 开始聊天按钮
                NAMEBOX: '.name', // 职位名称盒子
                JOBNAME: 'h1', // 职位名称
                SALARY: '.salary', // 职位薪资
                DETAIL: '.job-sec-text', // 职位详情
                COMPANY: '.job-detail-company .company-name, .sider-company .company-name, .company-info .company-name, .company-info a', // 公司名称
                CHATURL: 'redirect-url', // 聊天链接
                STARTCHAT_CANDIDATES: [
                    '.btn-startchat',
                    '[redirect-url][data-url]',
                ],
                JOBNAME_CANDIDATES: [
                    '.name h1',
                    '.job-primary .name h1',
                    '[class*="job-title"]',
                ],
                SALARY_CANDIDATES: [
                    '.name .salary',
                    '.job-primary .salary',
                    '[class*="salary"]',
                ],
                DESCRIPTION_CANDIDATES: [
                    '.job-sec-text',
                    '.job-detail-section .text',
                    '.job-detail .text',
                ],
                COMPANY_CANDIDATES: [
                    '.job-detail-company .company-name',
                    '.sider-company .company-name',
                    '.company-info .company-name',
                    '.company-info a',
                ],
            },
            CHAT: {
                // 聊天
                CHATINPUT: '#chat-input', // 聊天输入框
                MSGSEND: '.btn-send', // 消息发送按钮
                // 聊天记录
                HISTORYCTN: '.chat-message', // 聊天记录容器
                USEFULMSG: '.item-friend,.item-myself', // 有效的文字聊天记录项
                MSGCONTENT: '.message-content .text', // 聊天记录内容
                // 职位
                JOBEL: '*[ka=geek_chat_job_detail]', // 职位元素
                JOBCITY: '.city', // 职位城市
                // 简历
                RESUMESEND: '.toolbar-btn.tooltip.tooltip-top', // 简历发送按钮
                RESUMEMODAL: '.panel-resume', // 简历发送弹窗，有的时候简历按钮点击会出来一个小弹窗
                RESUMEMODALCONFIRM: '.btn-sure-v2', // 简历发送弹窗确认按钮
                RESUMELIST: '.resume-list', // 简历列表
                RESUMELISTITEM: 'li', // 简历列表项
                RESUMESENDCONFIRM: '.btn-confirm', // 简历发送确认按钮
                // 联系人
                CONTACTLISTEMPTY: '.no-data', // 联系人列表为空
                CONTACTLIST: '.user-list-content', // 联系人列表
                CONTACTLISTITEM: 'li', // 联系人列表项
                NEWMSGNOTICE: '.notice-badge', // 新消息通知图标
                USERNAME: '.name-text', // 联系人名称
            }
        },
    };

    // 搜索路径
    const SEARCHPATH = {
        zhipin: '/web/geek/job',
    };

    // 白名单
    const WHITELIST = {
        zhipin: {
            deatil: '/job_detail',
            chat: '/web/geek/chat'
        },
    };

    // 工具
    const tools = {
        inWhiteList: function (pathObj) {
            return Object.values(pathObj).some((path) => location.pathname.startsWith(path));
        },
        endlessFind: function (selector) {
            return new Promise((resolve, reject) => {
                // 初始立即检查元素是否存在
                let element;
                try {
                    element = document.querySelector(selector);
                } catch (e) {
                    reject(e); // 处理无效选择器
                    return;
                }
                if (element) {
                    resolve(element);
                    return;
                }

                // 设置超时
                const timeoutId = setTimeout(() => {
                    observer.disconnect();
                    reject(new Error('未找到目标元素'));
                }, 10000);

                // 定义MutationObserver回调
                const observer = new MutationObserver((_, obs) => {
                    try {
                        const el = document.querySelector(selector);
                        if (el) {
                            obs.disconnect();
                            clearTimeout(timeoutId);
                            resolve(el);
                        }
                    } catch (e) {
                        obs.disconnect();
                        clearTimeout(timeoutId);
                        reject(e);
                    }
                });

                // 开始观察整个文档的DOM变化
                observer.observe(document.documentElement, {
                    childList: true,
                    subtree: true
                });
            });
        },
        inputText: function (el, text) {
            el.value = text;
            el.dispatchEvent(new Event('input', { bubbles: true }));
        },
        asyncSleep(ms) {
            return new Promise((resolve) => {
                // 创建一个 Blob 对象，包含 Web Worker 的代码
                const workerCode = `self.addEventListener('message', function(e) {
                    const delay = e.data;
                    setTimeout(function() {
                        self.postMessage('done');
                    }, delay);
                });`;

                const blob = new Blob([workerCode], { type: 'application/javascript' });
                const workerUrl = URL.createObjectURL(blob);

                const worker = new Worker(workerUrl);
                worker.onmessage = function () {
                    resolve();
                    worker.terminate(); // 使用后终止worker
                    URL.revokeObjectURL(workerUrl); // 释放对象URL
                };
                worker.postMessage(ms);
            });
        },
        getTimestamp(key) {
            return Number(localStorage.getItem(key));
        },
        openTabNSetTimestamp(href, key, self = false) {
            localStorage.setItem(key, new Date().getTime());
            window.open(href, self ? '_self' : key);
        },
    };

    /**
     * 横幅
     * @param {string} text 显示的文本
     */
    function banner(text) {
        const el = document.createElement('div');
        el.style.cssText = `
                position: fixed;
                top: 60px;
                left: 50%;
                transform: translateX(-50%);
                z-index: 9999;
                background-color: rgba(0,0,0,.5);
                padding: 4px 20px;
                text-align: center;
                border-radius: 8px;
                color: #fff;
        `;
        el.innerText = text;
        document.body.appendChild(el);
        setTimeout(function () {
            el.remove();
        }, 3000);
    }

    /**
     * 转换时间
     * @param {number} seconds 秒数
     * @returns {string} 转换后的时间字符串
     */
    function convertTime(seconds) {
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const secs = seconds % 60;

        return `${hours.toString().padStart(2, 0)
            } : ${minutes.toString().padStart(2, 0)
            } : ${secs.toFixed(0).padStart(2, 0)
            }`;
    }


    class WebBroadcastError extends Error {
        constructor(code, message) {
            super(message);
            this.code = code;
            this.name = 'WebBroadcastError';
        }
    }

    class WebBroadcast {
        static ID_COUNTER = 0; // 自增序列，避免时间戳冲突

        /**
         * @param {string} name 频道名称
         * @param {string} target 当前页面标识
         * @param {object} [options] 配置项
         * @param {number} [options.retry=3] 发送失败重试次数
         * @param {number} [options.retryInterval=1000] 重试间隔(毫秒)
         */
        constructor(name, target, options = {}) {
            this.name = name;
            this.target = target;
            this.retry = options.retry ?? 3;
            this.retryInterval = options.retryInterval ?? 1000;
            this.evts = {};
            this.pendingResponses = {};
            this.pendingReceives = {};

            // 初始化通信通道
            this.initChannel();
        }

        /* -------------------- 核心通信逻辑 -------------------- */
        initChannel() {
            // 优先使用 BroadcastChannel
            if (typeof BroadcastChannel !== 'undefined') {
                this.setupBroadcastChannel();
            } else {
                this.setupStorageFallback();
            }
            window.addEventListener('beforeunload', () => this.destroy());
        }

        setupBroadcastChannel() {
            this.channelType = 'broadcast';
            this.channel = new BroadcastChannel(this.name);
            this.channel.addEventListener('message', this.handleMessage.bind(this));
            this.channel.addEventListener('messageerror', (e) => {
                this.emitError('MESSAGE_ERROR', '消息解析失败', e);
            });
        }

        setupStorageFallback() {
            this.channelType = 'storage';
            this.storageKey = `web_broadcast_${this.name}`;

            // 监听 storage 事件
            window.addEventListener('storage', (e) => {
                if (e.key === this.storageKey && e.newValue) {
                    const message = JSON.parse(e.newValue);
                    this.handleMessage({ data: message });
                }
            });
        }

        handleMessage(e) {
            const resp = e.data;
            if (![this.target, 'all'].includes(resp.to)) return;

            // 处理事件监听
            if (this.evts[resp.type]) {
                Promise.resolve().then(() => this.evts[resp.type](resp.from, resp.data));
            }

            // 处理 receive 等待
            const receiveKey = `${resp.from}-${resp.type}`;
            if (this.pendingReceives[receiveKey]) {
                const pending = this.pendingReceives[receiveKey];
                pending.resolve(resp.data);
                clearTimeout(pending.timer);
                delete this.pendingReceives[receiveKey];
            }

            // 处理 sendAndReceive 响应
            if (this.pendingResponses[resp.data?.requestId]) {
                const pending = this.pendingResponses[resp.data.requestId];
                pending.resolve(resp.data);
                clearTimeout(pending.timer);
                delete this.pendingResponses[resp.data.requestId];
            }
        }

        /* -------------------- 消息收发方法 -------------------- */
        send(to, type, data = null, attempt = 0) {
            const message = { from: this.target, to, type, data };

            return new Promise((resolve, reject) => {
                try {
                    if (this.channelType === 'broadcast') {
                        this.channel.postMessage(message);
                    } else {
                        // storage 方案需要先写入再删除，触发事件
                        localStorage.setItem(this.storageKey, JSON.stringify(message));
                        localStorage.removeItem(this.storageKey);
                    }
                    resolve();
                } catch (err) {
                    if (attempt < this.retry) {
                        setTimeout(() => this.send(to, type, data, attempt + 1), this.retryInterval);
                    } else {
                        this.emitError('SEND_FAILED', `消息发送失败: ${type}`, err);
                        reject(`消息发送失败: ${type}, ${err.message}`);
                    }
                }
            });
        }

        receive(from, type, timeout = 30000) {
            const key = `${from}-${type}`;
            return new Promise((resolve, reject) => {
                const timer = setTimeout(() => {
                    reject(new WebBroadcastError('TIMEOUT', `接收超时: ${type}`));
                    delete this.pendingReceives[key];
                }, timeout);

                this.pendingReceives[key] = { resolve, reject, timer };
            });
        }

        sendAndReceive(to, type, data = null, timeout = 30000) {
            const requestId = this.generateRequestId();
            const responseType = `${type}_response`;

            return new Promise((resolve, reject) => {
                const timer = setTimeout(() => {
                    reject(new WebBroadcastError('TIMEOUT', `请求超时: ${type}`));
                    delete this.pendingResponses[requestId];
                }, timeout);


                this.pendingResponses[requestId] = { resolve, reject, timer };
                // 发送时携带 responseType
                this.send(to, type, { ...data, requestId, responseType });
            });
        }

        reply(originalFrom, originalType, data, requestId, responseType) {
            const finalResponseType = responseType || `${originalType}_response`;
            return this.send(originalFrom, finalResponseType, { ...data, requestId });
        }

        /* -------------------- 工具方法 -------------------- */
        generateRequestId() {
            const time = Date.now().toString(36);
            const random = Math.random().toString(36).slice(2, 6);
            WebBroadcast.ID_COUNTER = (WebBroadcast.ID_COUNTER + 1) % 0xfff;
            return `${time}-${random}-${WebBroadcast.ID_COUNTER.toString(36).padStart(2, '0')}`;
        }

        emitError(code, message, error) {
            const err = new WebBroadcastError(code, `${message}: ${error?.message || error}`);
            console.error(err);
            if (this.evts['error']) {
                this.evts['error'](code, err.message);
            }
        }

        on(evt, fn) {
            if (typeof fn !== 'function') throw new Error('回调必须是函数');
            this.evts[evt] = fn;
        }

        off(evt) {
            delete this.evts[evt];
        }

        destroy() {
            if (this.channel) {
                this.channel.close();
            }
            window.removeEventListener('storage', this.handleMessage);
            this.pendingResponses = {};
            this.pendingReceives = {};
        }
    }

    // api请求
    class Api {
        constructor() { }

        /**
         * 封装请求
         * @param {string} path 请求路径
         * @param {string} method 请求方法
         * @param {any} data 请求数据
         * @returns {Promise<any>} 请求结果
         */
        __http(path, method = 'GET', data = null, timeoutMs = 1000 * 60 * 10) {
            const start = performance.now();
            return new Promise(async (resolve, reject) => {
                GM.xmlHttpRequest({
                    method: method,
                    url: OPTIONS.serverHost + path,
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    data: data,
                    timeout: timeoutMs,
                })
                    .then(resp => {
                        if (resp.status < 200 || resp.status >= 300) {
                            banner(`请求失败: ${resp.status}`);
                            reject(resp.status);
                            return;
                        }
                        resolve(JSON.parse(resp.response));
                    })
                    .catch((err) => {
                        banner('请求出错');
                        reject(`请求出错: ${JSON.stringify(err)}`);
                    });
            });
        }

        /**
         * 获取自我介绍
         */
        getIntroduce() {
            return new Promise((resolve, reject) => this.__http('/get-introduce').then(res => {
                resolve(res.introduce);
            }).catch(reject));
        }

        /**
         * 获取标签
         */
        getTags() {
            return new Promise((resolve, reject) => this.__http('/tags').then(res => {
                resolve(res.tags);
            }).catch(reject));
        }

        /**
         * 获取前端运行配置
         */
        getClientConfig() {
            return new Promise((resolve, reject) => this.__http('/client-config', 'GET', null, 5000).then(resolve).catch(reject));
        }

        /**
         * 获取职位匹配度
         * @param {string} title 职位标题
         * @param {string} salary 薪资范围
         * @param {string} detail 职位描述
         */
        getJobScore(title, salary, detail, searchKeyword = '') {
            const data = `# 职位名称\n${title}\n\n# 薪资范围\n${salary}\n\n# 职位描述\n${detail}`;
            const query = searchKeyword
                ? `?searchKeyword=${encodeURIComponent(searchKeyword)}`
                : '';
            return new Promise((resolve, reject) => {
                this.__http(`/get-job-score${query}`, 'POST', JSON.stringify(data)).then(resolve).catch(reject);
            });
        }

        /**
         * 回复消息
         * @param {string} msgs 消息记录
         */
        reply(msgs) {
            return new Promise((resolve, reject) => {
                this.__http('/reply', 'POST', JSON.stringify(msgs)).then(res => {
                    resolve(res);
                }).catch(reject);
            });
        }

        /**
         * 判断是否需要简历
         * @param {string} msgs 消息记录
         */
        isNeedResume(msgs) {
            return new Promise((resolve, reject) => {
                this.__http('/is-need-resume', 'POST', JSON.stringify(msgs)).then(res => {
                    resolve(res.need);
                }).catch(reject);
            });
        }

        /**
         * 判断是否需要作品集
         * @param {string} msgs 消息记录
         */
        isNeedWorks(msgs) {
            return new Promise((resolve, reject) => {
                this.__http('/is-need-works', 'POST', JSON.stringify(msgs)).then(res => {
                    resolve(res.need);
                }).catch(reject);
            });
        }

        /**
         * 记录动作日志
         * @param {object} payload 动作信息
         */
        logAction(payload) {
            return new Promise((resolve, reject) => {
                this.__http('/log-action', 'POST', JSON.stringify(payload)).then(resolve).catch(reject);
            });
        }

        createResearchProject(keyword, sourceUrl) {
            const capturedAt = new Date();
            const timestamp = capturedAt.toLocaleString('zh-CN', { hour12: false }).replaceAll('/', '-');
            return this.__http('/resume-agent/v1/candidates', 'POST', JSON.stringify({
                display_name: `JD调研-${keyword}-${timestamp}`.slice(0, 100),
                target_roles: [keyword],
                notes: `由Boss浏览器调研模式自动创建；关键词：${keyword}；来源：${sourceUrl}`,
            }));
        }

        collectResearchJob(payload) {
            return this.__http('/resume-agent/v1/research-jobs', 'POST', JSON.stringify(payload));
        }
    }

    // 日志记录
    class Logger {
        constructor(startFn, pauseFn, enableModeSelector = false) {
            // 校验函数
            if (startFn && !Function.prototype.isPrototypeOf(startFn)) {
                throw new Error('参数错误，startFn应为函数');
            }
            if (pauseFn && !Function.prototype.isPrototypeOf(pauseFn)) {
                throw new Error('参数错误，pauseFn应为函数');
            }
            // 创建元素
            const ctn = document.createElement('div');
            const btnBox = document.createElement('div');
            const clearBtn = document.createElement('div');
            const runBtn = document.createElement('div');
            const foldBtn = document.createElement('div');
            const settingsHeader = document.createElement('div');
            const versionLabel = document.createElement('span');
            const modeBox = document.createElement('div');
            const modeLabel = document.createElement('span');
            const modeControl = document.createElement('div');
            const deliveryModeBtn = document.createElement('button');
            const researchModeBtn = document.createElement('button');
            const keywordBox = document.createElement('label');
            const keywordLabel = document.createElement('span');
            const keywordInput = document.createElement('textarea');
            const preloadBox = document.createElement('label');
            const preloadLabel = document.createElement('span');
            const preloadInput = document.createElement('input');
            const msgList = document.createElement('div');
            ctn.style.cssText = `
                position: fixed;
                bottom: 16px;
                left: 16px;
                width: 380px;
                background-color: rgba(0, 0, 0, 0.5);
                color: #fff;
                z-index: 9999;
                font-size: 14px;
                border-radius: 8px;
                overflow: hidden;
            `;
            btnBox.style.cssText = `
                width: 380px;
                height: 32px;
                display: flex;
                align-items: center;
                justify-content: flex-end;
            `;
            clearBtn.style.cssText = runBtn.style.cssText = foldBtn.style.cssText = `
                width: 60px;
                height: 32px;
                line-height: 32px;
                text-align: center;
                cursor: pointer;
            `;
            msgList.style.cssText = `
                width: 380px;
                height: 240px;
                padding: 2px 12px 8px;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 4px;
            `;
            clearBtn.innerText = "清空";
            runBtn.innerText = "开始";
            foldBtn.innerText = "收起";
            settingsHeader.style.cssText = `
                display: flex;
                align-items: center;
                justify-content: space-between;
                min-height: 32px;
                padding: 0 12px;
                background: rgba(0, 0, 0, 0.22);
                border-bottom: 1px solid rgba(255, 255, 255, 0.18);
                box-sizing: border-box;
            `;
            versionLabel.innerText = `goodJobs ${SCRIPT_VERSION}`;
            versionLabel.style.cssText = `
                color: rgba(255, 255, 255, 0.86);
                font-size: 12px;
            `;
            modeBox.style.cssText = `
                display: flex;
                align-items: center;
                gap: 12px;
                width: 100%;
                min-height: 44px;
                padding: 6px 12px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.2);
                box-sizing: border-box;
            `;
            modeLabel.innerText = '运行模式';
            modeLabel.style.cssText = `
                width: 64px;
                flex: 0 0 64px;
                color: rgba(255, 255, 255, 0.92);
            `;
            modeControl.style.cssText = `
                display: flex;
                flex: 1;
                height: 32px;
                border: 1px solid rgba(255, 255, 255, 0.28);
                border-radius: 6px;
                overflow: hidden;
            `;
            deliveryModeBtn.type = 'button';
            researchModeBtn.type = 'button';
            deliveryModeBtn.innerText = '投递';
            researchModeBtn.innerText = '调研';
            [deliveryModeBtn, researchModeBtn].forEach(button => {
                button.style.cssText = `
                    flex: 1;
                    border: 0;
                    border-radius: 0;
                    color: white;
                    background: transparent;
                    cursor: pointer;
                    font-size: 13px;
                    letter-spacing: 0;
                `;
            });
            keywordBox.style.cssText = `
                display: flex;
                align-items: flex-start;
                gap: 12px;
                width: 100%;
                padding: 8px 12px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.2);
                box-sizing: border-box;
            `;
            keywordLabel.innerText = '搜索岗位';
            keywordLabel.style.cssText = `
                width: 64px;
                flex: 0 0 64px;
                padding-top: 7px;
                color: rgba(255, 255, 255, 0.92);
            `;
            keywordInput.rows = 3;
            keywordInput.placeholder = 'AI应用工程师\n智能体工程师';
            keywordInput.style.cssText = `
                display: block;
                flex: 1;
                min-width: 0;
                min-height: 68px;
                max-height: 160px;
                padding: 7px 9px;
                border: 1px solid rgba(255, 255, 255, 0.32);
                border-radius: 6px;
                outline: none;
                resize: vertical;
                box-sizing: border-box;
                color: #fff;
                background: rgba(0, 0, 0, 0.28);
                font: inherit;
                line-height: 1.45;
                letter-spacing: 0;
            `;
            preloadBox.style.cssText = `
                display: flex;
                align-items: center;
                gap: 12px;
                width: 100%;
                min-height: 44px;
                padding: 6px 12px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.2);
                box-sizing: border-box;
            `;
            preloadLabel.innerText = '预加载上限（轮/关键词）';
            preloadLabel.style.cssText = `
                width: 148px;
                flex: 0 0 148px;
                color: rgba(255, 255, 255, 0.92);
            `;
            preloadInput.type = 'number';
            preloadInput.min = String(MIN_PRELOAD_MAX_ROUNDS);
            preloadInput.max = String(MAX_PRELOAD_MAX_ROUNDS);
            preloadInput.step = '1';
            preloadInput.value = String(DEFAULT_PRELOAD_MAX_ROUNDS);
            preloadInput.style.cssText = `
                display: block;
                flex: 1;
                min-width: 0;
                height: 32px;
                padding: 5px 9px;
                border: 1px solid rgba(255, 255, 255, 0.32);
                border-radius: 6px;
                outline: none;
                box-sizing: border-box;
                color: #fff;
                background: rgba(0, 0, 0, 0.28);
                font: inherit;
                letter-spacing: 0;
            `;
            document.body.appendChild(ctn);
            if (enableModeSelector) {
                ctn.appendChild(settingsHeader);
                settingsHeader.appendChild(versionLabel);
                ctn.appendChild(modeBox);
                modeBox.appendChild(modeLabel);
                modeBox.appendChild(modeControl);
                modeControl.appendChild(deliveryModeBtn);
                modeControl.appendChild(researchModeBtn);
                ctn.appendChild(keywordBox);
                keywordBox.appendChild(keywordLabel);
                keywordBox.appendChild(keywordInput);
                ctn.appendChild(preloadBox);
                preloadBox.appendChild(preloadLabel);
                preloadBox.appendChild(preloadInput);
            }
            ctn.appendChild(btnBox);
            btnBox.appendChild(clearBtn);
            btnBox.appendChild(runBtn);
            btnBox.appendChild(foldBtn);
            ctn.appendChild(msgList);
            this.ctn = ctn;
            this.list = msgList;
            this.runBtn = runBtn;
            this.clearBtn = clearBtn;
            this.enableModeSelector = enableModeSelector;
            this.mode = 'research';
            this.modeLocked = false;
            this.modeBox = modeBox;
            this.deliveryModeBtn = deliveryModeBtn;
            this.researchModeBtn = researchModeBtn;
            this.keywordInput = keywordInput;
            this.keywordEdited = false;
            this.preloadInput = preloadInput;
            this.preloadEdited = false;
            this.__startFn = startFn || (() => void 0);
            this.__pauseFn = pauseFn || (() => void 0);
            this.__pause = true;
            clearBtn.addEventListener('click', () => this.clear());
            deliveryModeBtn.addEventListener('click', () => this.setMode('delivery'));
            researchModeBtn.addEventListener('click', () => this.setMode('research'));
            keywordInput.addEventListener('input', () => {
                this.keywordEdited = true;
                keywordInput.style.borderColor = 'rgba(255, 255, 255, 0.32)';
                localStorage.setItem(SEARCH_KEYWORDS_STORAGE_KEY, keywordInput.value);
            });
            preloadInput.addEventListener('input', () => {
                this.preloadEdited = true;
                preloadInput.style.borderColor = 'rgba(255, 255, 255, 0.32)';
            });
            preloadInput.addEventListener('change', () => {
                this.setPreloadMaxRounds(preloadInput.value, true);
            });
            this.setMode('research');
            runBtn.addEventListener('click', () => {
                if (this.__pause && this.enableModeSelector && !this.getKeywords().length) {
                    keywordInput.style.borderColor = '#ff6b6b';
                    keywordInput.focus();
                    this.add('请至少填写一个搜索岗位');
                    return;
                }
                this.__pause = !this.__pause;
                if (this.__pause) {
                    runBtn.innerText = "继续";
                    this.__pauseFn();
                } else {
                    runBtn.innerText = "暂停";
                    this.__startFn();
                }
            });
            foldBtn.addEventListener('click', () => {
                if (foldBtn.innerText === "展开") {
                    msgList.style.height = "240px";
                    foldBtn.innerText = "收起";
                } else {
                    msgList.style.height = "32px";
                    this.list.scrollTop = this.list.scrollHeight;
                    foldBtn.innerText = "展开";
                }
            });
        }

        setMode(mode) {
            if (this.modeLocked || !['delivery', 'research'].includes(mode)) return;
            this.mode = mode;
            const selectedStyle = 'rgba(0, 174, 120, 0.95)';
            const idleStyle = 'transparent';
            this.deliveryModeBtn.style.background = mode === 'delivery' ? selectedStyle : idleStyle;
            this.researchModeBtn.style.background = mode === 'research' ? selectedStyle : idleStyle;
        }

        getMode() {
            return this.mode;
        }

        setKeywords(keywords, persist = false) {
            const normalized = parseSearchKeywords(keywords);
            this.keywordInput.value = normalized.join('\n');
            if (persist) {
                localStorage.setItem(SEARCH_KEYWORDS_STORAGE_KEY, this.keywordInput.value);
            }
        }

        getKeywords() {
            return parseSearchKeywords(this.keywordInput.value);
        }

        hasKeywordEdits() {
            return this.keywordEdited;
        }

        setPreloadMaxRounds(value, persist = false) {
            const normalized = normalizePreloadMaxRounds(value);
            this.preloadInput.value = String(normalized);
            if (persist) {
                localStorage.setItem(PRELOAD_MAX_ROUNDS_STORAGE_KEY, String(normalized));
            }
            return normalized;
        }

        getPreloadMaxRounds() {
            return normalizePreloadMaxRounds(this.preloadInput.value);
        }

        hasPreloadEdits() {
            return this.preloadEdited;
        }

        lockMode() {
            this.modeLocked = true;
            this.modeBox.style.opacity = '0.75';
            this.deliveryModeBtn.style.cursor = 'default';
            this.researchModeBtn.style.cursor = 'default';
        }

        lockSettings() {
            this.lockMode();
            this.keywordInput.disabled = true;
            this.keywordInput.style.cursor = 'default';
            this.keywordInput.style.opacity = '0.78';
            this.preloadInput.disabled = true;
            this.preloadInput.style.cursor = 'default';
            this.preloadInput.style.opacity = '0.78';
        }

        resetStart() {
            this.__pause = true;
            this.runBtn.innerText = '开始';
        }

        complete(text = '已完成') {
            this.__pause = true;
            this.runBtn.innerText = text;
            this.runBtn.style.pointerEvents = 'none';
            this.runBtn.style.opacity = '0.75';
        }

        add(message) {
            const item = document.createElement('div');
            item.textContent = message;
            this.list.appendChild(item);
            this.list.scrollTop = this.list.scrollHeight;
        }

        divider() {
            const item = document.createElement('div');
            item.style.cssText = `
                width: 100%;
                border-top: 1px dashed rgba(255, 255, 255, 0.6);
            `;
            this.list.appendChild(item);
            this.list.scrollTop = this.list.scrollHeight;
        }

        clear() {
            while (this.list.firstChild) {
                this.list.removeChild(this.list.firstChild);
            }
        }

        remove() {
            this.ctn.remove();
        }
    }

    // boss 直聘
    class Zhipin {
        constructor() {
            // 窗口标签
            this.targets = {
                search: "__zhipin_search",
                detail: "__zhipin_detail",
                chat: "__zhipin_chat",
                chatGreet: "__zhipin_chat_greet",
            };
            // 广播类型
            this.bcTypes = {
                // 全局
                STATUS: "status",
                RUN: 'run',
                DIVIDER: 'divider',
                INTRODUCE: 'introduce',
                HEART_BEAT: 'heart-beat',
                // 聊天页和职位详情页
                GET_JOB_INFO: 'get-job-info',
                SAY_HI: 'say-hi',
            };
            // 白名单
            this.whiteList = WHITELIST.zhipin;
            // 记录状态
            this.pause = false;
            this.tags = [];
            this.introduce = ''
        }

        // 注册广播
        __broadcast(target) {
            this.broadcast = new WebBroadcast('__zhipin_broadcast', target);
        }

        // 搜索页
        async __search(tagIdx) {
            // api
            const api = new Api();
            // 记录开始时间
            const start = new Date().getTime();
            let count = 0;
            let page = 0;
            // 记录职位链接
            let jobHrefs = [];
            let elsLen = 0;
            // 缓存
            let started = false;
            let pendingRoundRestart = false;
            let roundTransitioning = false;
            let currentRound = 0;
            let emptyRounds = 0;
            let roundQueuedCount = 0;
            let currentKeyword = '';
            let currentTagIdx = -1;
            let activeMode = 'research';
            let activePreloadMaxRounds = DEFAULT_PRELOAD_MAX_ROUNDS;
            let currentResearchProject = null;
            let currentResearchStats = null;
            const researchSummaries = [];
            const processedJobHrefs = new Set();

            // 日志启动暂停事件
            const logger = new Logger(() => {
                this.pause = false;
                if (!started) return main();
                if (pendingRoundRestart) {
                    pendingRoundRestart = false;
                    return startRound();
                }
                loop();
            }, () => {
                this.pause = true;
            }, true);
            const storedKeywordText = localStorage.getItem(SEARCH_KEYWORDS_STORAGE_KEY) || '';
            const storedKeywords = parseSearchKeywords(storedKeywordText);
            logger.setKeywords(storedKeywords.length ? storedKeywords : FALLBACK_SEARCH_KEYWORDS);
            const storedPreloadMaxRounds = localStorage.getItem(PRELOAD_MAX_ROUNDS_STORAGE_KEY);
            const hasStoredPreloadMaxRounds = storedPreloadMaxRounds !== null;
            logger.setPreloadMaxRounds(
                hasStoredPreloadMaxRounds ? storedPreloadMaxRounds : OPTIONS.preloadMaxRounds,
                hasStoredPreloadMaxRounds,
            );
            const bootstrapServerHost = OPTIONS.serverHost;
            const clientConfigPromise = api.getClientConfig().then(clientConfig => {
                if (clientConfig && clientConfig.frontend) {
                    const frontendConfig = { ...clientConfig.frontend };
                    delete frontendConfig.serverHost;
                    Object.assign(OPTIONS, frontendConfig);
                    OPTIONS.serverHost = bootstrapServerHost;
                    OPTIONS.preloadStableRoundsLimit = PRELOAD_STABLE_ROUNDS_LIMIT;
                }
                if (hasStoredPreloadMaxRounds) {
                    OPTIONS.preloadMaxRounds = logger.getPreloadMaxRounds();
                } else if (!logger.hasPreloadEdits()) {
                    OPTIONS.preloadMaxRounds = logger.setPreloadMaxRounds(OPTIONS.preloadMaxRounds);
                }
                if (!storedKeywords.length && !logger.hasKeywordEdits() && Array.isArray(clientConfig?.tags) && clientConfig.tags.length) {
                    logger.setKeywords(clientConfig.tags);
                }
                return clientConfig;
            }).catch((e) => {
                logger.add(`获取后端配置失败，将使用当前面板设置：${e}`);
                OPTIONS.serverHost = bootstrapServerHost;
                return null;
            });

            // 开始广播
            const startBroadcast = () => {
                this.__broadcast(this.targets.search);
                // 接收聊天页的消息提醒
                this.broadcast.on(this.bcTypes.STATUS, (from, data) => {
                    if (from === this.targets.chat) {
                        logger.add(data);
                    }
                });
                // 发送自我介绍
                this.broadcast.on(this.bcTypes.INTRODUCE, (from, data) => {
                    this.broadcast.reply(
                        from,
                        this.bcTypes.INTRODUCE,
                        { introduce: this.introduce },
                        data.requestId,
                        data.responseType
                    );
                });
                // 分割线
                this.broadcast.on(this.bcTypes.DIVIDER, () => {
                    logger.divider();
                });
                if (activeMode === 'delivery') {
                    // 调研模式不注册任何打招呼或聊天协作逻辑。
                    greetListener();
                    chatListener();
                    heartBeatListener();
                }
            };

            // 执行搜索
            const search = async (kw) => {
                try {
                    const input = await tools.endlessFind(SELECTORS.ZHIPIN.SEARCH.SEARCHINPUT);
                    const btn = await tools.endlessFind(SELECTORS.ZHIPIN.SEARCH.SEARCHBTN);
                    tools.inputText(input, kw);
                    btn.click();
                } catch (e) {
                    logger.add('搜索出错');
                    throw new Error('搜索出错');
                }
            };

            // 获取职位链接
            const getJobHrefs = async () => {
                try {
                    const jobUl = await tools.endlessFind(SELECTORS.ZHIPIN.SEARCH.JOBLIST);
                    const aList = safeQueryAll(jobUl, SELECTORS.ZHIPIN.SEARCH.JOBHREFS);
                    const hrefs = aList
                        .slice(elsLen)
                        .map(a => {
                            const href = (typeof a?.href === 'string' ? a.href : '') || safeAttribute(a, 'href');
                            if (!href) return null;
                            let card = null;
                            try {
                                card = typeof a.closest === 'function'
                                    ? a.closest(SELECTORS.ZHIPIN.SEARCH.JOBCARD)
                                    : null;
                            } catch (e) {
                                card = null;
                            }
                            return {
                                href,
                                company: safeText(card, SELECTORS.ZHIPIN.SEARCH.COMPANY),
                            };
                        })
                        .filter(Boolean)
                        .filter(job => !processedJobHrefs.has(job.href));
                    return { available: true, hrefs, elements: aList };
                } catch (e) {
                    logger.add('当前页面未找到可读取的岗位列表，已跳过本轮');
                    return { available: false, hrefs: [], elements: [] };
                }
            };

            const resetRoundState = () => {
                jobHrefs = [];
                elsLen = 0;
                page = 0;
                roundQueuedCount = 0;
                currentResearchProject = null;
                currentResearchStats = null;
                if (activeMode === 'research') {
                    processedJobHrefs.clear();
                }
                clearPendingGreet();
            };

            const activatePreloadCard = async (round) => {
                if (!OPTIONS.preloadActivateCardEvery || round % OPTIONS.preloadActivateCardEvery !== 0) return;
                try {
                    const jobUl = document.querySelector(SELECTORS.ZHIPIN.SEARCH.JOBLIST);
                    if (!jobUl) return;
                    const cards = Array.from(jobUl.querySelectorAll(SELECTORS.ZHIPIN.SEARCH.JOBCARD));
                    if (!cards.length) return;
                    const visibleCards = cards.filter(card => {
                        const rect = card.getBoundingClientRect();
                        return rect.top < window.innerHeight - 120 && rect.bottom > 120;
                    });
                    const targetCard = visibleCards[visibleCards.length - 1] || cards[cards.length - 1];
                    if (!targetCard) return;
                    targetCard.scrollIntoView({ block: 'center', behavior: 'smooth' });
                    await tools.asyncSleep(120);
                    targetCard.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    targetCard.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    targetCard.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    logger.add(`预加载第 ${round} 轮：已轻点左侧岗位卡片`);
                    await tools.asyncSleep(OPTIONS.preloadActivateCardWaitMs);
                } catch (e) {
                    logger.add('预加载时轻点岗位卡片失败，已继续纯滚动');
                }
            };

            // 下一页
            const nextPage = async () => {
                while (true) {
                    const pageJobs = await getJobHrefs();
                    if (!pageJobs.available) return false;
                    const { hrefs, elements: els } = pageJobs;
                    if (els.length === elsLen) {
                        logger.add('没有更多职位了');
                        return false;
                    }
                    elsLen = els.length;
                    els[elsLen - 1].scrollIntoView();
                    page++;
                    logger.add(`开始浏览第 ${page} 页`);
                    if (hrefs.length) {
                        jobHrefs.push(...hrefs);
                        roundQueuedCount += hrefs.length;
                        logger.add(`本页新增 ${hrefs.length} 个未处理岗位`);
                        return true;
                    }
                    logger.add('本页新增岗位都已处理过，继续向下查找');
                    await tools.asyncSleep(OPTIONS.preloadScrollWaitMs);
                }
            };

            document.nextPage = nextPage

            let pendingGreetTimer = null;
            let pendingGreetTitle = '';
            let pendingGreetDecision = null;

            const clearPendingGreet = () => {
                if (pendingGreetTimer) {
                    clearTimeout(pendingGreetTimer);
                    pendingGreetTimer = null;
                }
                pendingGreetTitle = '';
                pendingGreetDecision = null;
            };

            const armPendingGreet = (title, decision = null) => {
                clearPendingGreet();
                pendingGreetTitle = title;
                pendingGreetDecision = decision;
                pendingGreetTimer = setTimeout(() => {
                    logger.add(`职位 [${pendingGreetTitle}] 打招呼超时，已跳过`);
                    clearPendingGreet();
                    loop();
                }, OPTIONS.greetTimeout);
            };

            const summarizeResearchKeyword = () => {
                if (!currentResearchStats) return;
                researchSummaries.push({ ...currentResearchStats });
                logger.add(
                    `关键词 [${currentResearchStats.keyword}] 完成 | ` +
                    `项目: ${currentResearchStats.candidateId} | ` +
                    `扫描: ${currentResearchStats.scanned} | ` +
                    `入库: ${currentResearchStats.created} | ` +
                    `重复: ${currentResearchStats.duplicate} | ` +
                    `字段缺失: ${currentResearchStats.incomplete} | ` +
                    `失败: ${currentResearchStats.failed}`
                );
            };

            const finishResearch = () => {
                this.pause = true;
                logger.divider();
                logger.add('调研采集完成，所有关键词均已执行一次');
                researchSummaries.forEach(summary => {
                    logger.add(
                        `[${summary.keyword}] 项目 ${summary.candidateId} | ` +
                        `入库 ${summary.created} | 重复 ${summary.duplicate} | ` +
                        `字段缺失 ${summary.incomplete} | 失败 ${summary.failed}`
                    );
                });
                logger.complete('已完成');
            };

            const handleRoundExhausted = async () => {
                if (roundTransitioning) return;
                roundTransitioning = true;
                try {
                    if (activeMode === 'research') {
                        summarizeResearchKeyword();
                        if (currentRound >= this.tags.length) {
                            finishResearch();
                            return;
                        }
                        await tools.asyncSleep(OPTIONS.roundRestartDelayMs);
                        if (this.pause) {
                            pendingRoundRestart = true;
                            logger.add('当前已暂停，下一关键词等待继续');
                            return;
                        }
                        await startRound();
                        return;
                    }
                    if (roundQueuedCount === 0) {
                        emptyRounds += 1;
                        logger.add(`第 ${currentRound} 轮没有拿到新岗位（连续空轮 ${emptyRounds}/${OPTIONS.maxEmptyRounds}）`);
                    } else {
                        emptyRounds = 0;
                        logger.add(`第 ${currentRound} 轮已处理完当前加载岗位，准备进入下一轮`);
                    }
                    if (emptyRounds >= OPTIONS.maxEmptyRounds) {
                        logger.add(`连续 ${OPTIONS.maxEmptyRounds} 轮没有新岗位，自动切换到下一个关键词继续挂机`);
                        emptyRounds = 0;
                        return startRound();
                    }
                    await tools.asyncSleep(OPTIONS.roundRestartDelayMs);
                    if (this.pause) {
                        pendingRoundRestart = true;
                        logger.add('当前已暂停，下一轮等待继续');
                        return;
                    }
                    await startRound();
                } finally {
                    roundTransitioning = false;
                }
            };

            const logAction = async (payload) => {
                try {
                    await api.logAction(payload);
                } catch (e) {
                    console.log('logAction failed', e);
                }
            };

            // 获取职位信息
            const getJobInfo = async (href) => {
                // 打开窗口
                tools.openTabNSetTimestamp(href, this.targets.detail);
                // 接收职位信息
                const info = await this.broadcast.receive(
                    this.targets.detail,
                    this.bcTypes.GET_JOB_INFO,
                    OPTIONS.detailTimeout
                ).catch(() => ({
                    title: '',
                    company: '',
                    salary: '',
                    detail: '',
                    skip: true,
                    skipReason: `获取职位详情超时（>${(OPTIONS.detailTimeout / 1000).toFixed(0)}s）`,
                }));
                if (!info || typeof info !== 'object') {
                    return {
                        title: '',
                        company: '',
                        salary: '',
                        detail: '',
                        skip: true,
                        skipReason: '详情页未返回可读取的职位信息',
                    };
                }
                return info;
            };

            // 添加到聊天列表
            const addToChatList = async (url) => {
                return new Promise((resolve, reject) => {
                    fetch(url)
                        .then(async resp => {
                            if (!(resp.ok && resp.status === 200)) {
                                const bodyText = await resp.text().catch(() => '');
                                logger.add(`boss直聘网络连接出错: status=${resp.status}`);
                                return reject(new Error(`http_${resp.status}:${bodyText.slice(0, 300)}`));
                            }
                            return resp.json();
                        }).then(resp => {
                            if (resp.code === 0) return resolve(resp);
                            const msg = resp?.zpData?.bizData?.chatRemindDialog?.title || resp?.message || '未知错误';
                            logger.add(`打招呼失败: ${msg}`);
                            reject(new Error(`biz_fail:${msg}`));
                        }).catch(err => {
                            reject(err instanceof Error ? err : new Error(String(err)));
                        });
                });
            };

            // 打招呼监听
            const greetListener = () => {
                this.broadcast.on(this.bcTypes.SAY_HI, async (from, data) => {
                    if (from !== this.targets.chatGreet) return;
                    // 要自我介绍
                    if (data.requestId) {
                        this.broadcast.reply(
                            from,
                            this.bcTypes.SAY_HI,
                            {
                                introduce: pendingGreetDecision?.introduce || this.introduce,
                                resumeIndex: pendingGreetDecision?.resumeIndex ?? OPTIONS.resumeIndex,
                            },
                            data.requestId,
                            data.responseType
                        );
                        return;
                    }
                    // 告知结果
                    const finalDecision = pendingGreetDecision;
                    const finalTitle = pendingGreetTitle;
                    clearPendingGreet();
                    if (data.success) {
                        logger.add(`打招呼成功`);
                        await logAction({
                            action: 'greet_sent',
                            scene: 'search',
                            title: finalTitle,
                            resumeIndex: finalDecision?.resumeIndex ?? OPTIONS.resumeIndex,
                        });
                    }
                    // 出错了
                    else {
                        logger.add(`打招呼失败`);
                        await logAction({
                            action: 'greet_failed',
                            scene: 'search',
                            title: finalTitle,
                            resumeIndex: finalDecision?.resumeIndex ?? OPTIONS.resumeIndex,
                        });
                    }
                    loop();
                });
            };

            // 聊天页监听
            const chatListener = () => {
                this.broadcast.on(this.bcTypes.RUN, async (from, data) => {
                    if (from !== this.targets.chat) return;
                    if (data) {
                        logger.divider();
                        const hasNext = await nextPage();
                        if (!hasNext) return handleRoundExhausted();
                        loop();
                    } else {
                        logger.add(`消息处理出错，重试中...`);
                        tools.openTabNSetTimestamp(this.whiteList.chat, this.targets.chat);
                    }
                });
            };

            // 心跳监听
            const heartBeatListener = () => {
                this.broadcast.on(this.bcTypes.HEART_BEAT, async (from, data) => {
                    this.broadcast.reply(
                        from,
                        this.bcTypes.HEART_BEAT,
                        { success: true },
                        data.requestId,
                        data.responseType
                    );
                });
            }

            // 循环
            const loop = async () => {
                try {
                    // 如果暂停，则跳过
                    if (this.pause) {
                        logger.add('暂停中...');
                        return;
                    }
                    logger.divider();
                    // 判断职位链接是否为空
                    if (jobHrefs.length === 0) {
                        if (activeMode === 'research') {
                            const hasNext = await nextPage();
                            if (!hasNext) return handleRoundExhausted();
                            return loop();
                        }
                        // 判断是否需要代聊天
                        if (OPTIONS.onlyGreet) {
                            const hasNext = await nextPage();
                            if (!hasNext) return handleRoundExhausted();
                            return loop();
                        }
                        logger.add('开始处理聊天消息');
                        tools.openTabNSetTimestamp(this.whiteList.chat, this.targets.chat);
                        return;
                    }
                    // 抽取第一个
                    const jobEntry = jobHrefs.shift();
                    const href = typeof jobEntry === 'string' ? jobEntry : jobEntry.href;
                    const cardCompany = typeof jobEntry === 'string' ? '' : jobEntry.company;
                    const diff = (new Date().getTime() - start) / 1000;
                    // 获取详情
                    logger.add(`| 浏览: ${++count} | 剩余: ${jobHrefs.length} | 平均: ${(diff / count).toFixed(0)}s | 耗时: ${convertTime(diff)} |`);
                    logger.add(`正在获取职位详情`);
                    const jobInfo = await getJobInfo(href);
                    processedJobHrefs.add(href);
                    if (activeMode === 'research') {
                        currentResearchStats.scanned += 1;
                        const researchJob = {
                            candidate_id: currentResearchProject.id,
                            external_job_id: href,
                            title: (jobInfo.title || '').trim(),
                            company: (jobInfo.company || cardCompany || '').trim(),
                            salary: (jobInfo.salary || '').trim(),
                            detail: (jobInfo.detail || '').trim(),
                            search_keyword: currentKeyword,
                        };
                        const missingFields = ['title', 'company', 'salary', 'detail']
                            .filter(field => !researchJob[field]);
                        if (missingFields.length) {
                            currentResearchStats.incomplete += 1;
                            const reason = jobInfo.skipReason
                                ? `；${jobInfo.skipReason}`
                                : '';
                            logger.add(`调研岗位未入库，缺少字段：${missingFields.join('、')}${reason}`);
                            return loop();
                        }
                        try {
                            const collected = await api.collectResearchJob(researchJob);
                            if (collected.created) {
                                currentResearchStats.created += 1;
                                logger.add(`已采集 [${researchJob.company}] ${researchJob.title}`);
                            } else {
                                currentResearchStats.duplicate += 1;
                                logger.add(`重复岗位已跳过 [${researchJob.company}] ${researchJob.title}`);
                            }
                        } catch (e) {
                            currentResearchStats.failed += 1;
                            logger.add(`岗位入库失败 [${researchJob.title}]：${e}`);
                        }
                        return loop();
                    }
                    if (jobInfo.skip) {
                        logger.add(`职位跳过: ${jobInfo.skipReason}`);
                        await logAction({
                            action: 'job_skip',
                            scene: 'search',
                            title: jobInfo.title || null,
                            salary: jobInfo.salary || null,
                            detail: jobInfo.detail || null,
                            reason: jobInfo.skipReason,
                        });
                        return loop();
                    }
                    // 如果聊过，下一个
                    if (jobInfo.talked) {
                        logger.add(`职位 [${jobInfo.title}] 已经聊过，下一个`);
                        await logAction({
                            action: 'job_already_talked',
                            scene: 'search',
                            title: jobInfo.title,
                            salary: jobInfo.salary,
                        });
                        return loop();
                    }
                    // 否则发送消息计算匹配度
                    logger.add(`开始计算职位 [${jobInfo.title}] 的匹配度`);
                    const decision = await api.getJobScore(
                        jobInfo.title,
                        jobInfo.salary,
                        jobInfo.detail,
                        currentKeyword
                    );
                    logger.add(`匹配度: ${decision.score} | 简历索引: ${decision.resumeIndex}`);
                    await logAction({
                        action: 'job_decision_consumed',
                        scene: 'search',
                        title: jobInfo.title,
                        salary: jobInfo.salary,
                        score: decision.score,
                        resumeIndex: decision.resumeIndex,
                    });
                    // 如果分数达到阈值，打个招呼
                    if (decision.score >= OPTIONS.thread) {
                        logger.add(`正在给职位 [${jobInfo.title}] 发送打招呼消息`);
                        await logAction({
                            action: 'greet_queued',
                            scene: 'search',
                            title: jobInfo.title,
                            salary: jobInfo.salary,
                            resumeIndex: decision.resumeIndex,
                            score: decision.score,
                        });
                        // 判断是否有提醒返回
                        addToChatList(jobInfo.addUrl).then(async () => {
                            await logAction({
                                action: 'chat_open_requested',
                                scene: 'search',
                                title: jobInfo.title,
                                chatUrl: jobInfo.chatUrl,
                                resumeIndex: decision.resumeIndex,
                            });
                            armPendingGreet(jobInfo.title, decision);
                            tools.openTabNSetTimestamp(jobInfo.chatUrl, this.targets.chatGreet);
                        }).catch(async (err) => {
                            await logAction({
                                action: 'greet_queue_failed',
                                scene: 'search',
                                title: jobInfo.title,
                                resumeIndex: decision.resumeIndex,
                                addUrl: jobInfo.addUrl,
                                chatUrl: jobInfo.chatUrl,
                                reason: String(err),
                            });
                            clearPendingGreet();
                            loop();
                        });
                    }
                    // 否则下一轮
                    else {
                        await logAction({
                            action: 'job_below_threshold',
                            scene: 'search',
                            title: jobInfo.title,
                            salary: jobInfo.salary,
                            score: decision.score,
                            threshold: OPTIONS.thread,
                            resumeIndex: decision.resumeIndex,
                        });
                        loop();
                    }
                } catch (e) {
                    console.log(e);
                    logger.add(`循环时出错: ${e}`);
                    loop();
                }
            };

            const preloadJobs = async () => {
                logger.add('开始慢速预加载岗位列表');
                let stableRounds = 0;
                let highestCount = null;
                for (let round = 1; round <= activePreloadMaxRounds; round++) {
                    const jobUl = await tools.endlessFind(SELECTORS.ZHIPIN.SEARCH.JOBLIST).catch(() => null);
                    const currentCount = jobUl ? jobUl.querySelectorAll(SELECTORS.ZHIPIN.SEARCH.JOBHREFS).length : 0;
                    highestCount = highestCount === null ? currentCount : Math.max(highestCount, currentCount);
                    window.scrollBy({ top: OPTIONS.preloadScrollPixels, left: 0, behavior: 'smooth' });
                    await tools.asyncSleep(OPTIONS.preloadScrollWaitMs);
                    await activatePreloadCard(round);
                    const afterJobUl = document.querySelector(SELECTORS.ZHIPIN.SEARCH.JOBLIST);
                    const afterCount = afterJobUl ? afterJobUl.querySelectorAll(SELECTORS.ZHIPIN.SEARCH.JOBHREFS).length : currentCount;
                    logger.add(`预加载第 ${round} 轮：岗位 ${currentCount} -> ${afterCount}`);
                    if (afterCount > highestCount) {
                        stableRounds = 0;
                    } else {
                        stableRounds += 1;
                    }
                    highestCount = Math.max(highestCount, afterCount);
                    if (stableRounds >= OPTIONS.preloadStableRoundsLimit) {
                        logger.add(`预加载结束：连续 ${stableRounds} 轮无新增岗位`);
                        break;
                    }
                    if (round === activePreloadMaxRounds) {
                        logger.add(`预加载结束：已达到用户设置的 ${activePreloadMaxRounds} 轮上限`);
                    }
                }
                const finalJobUl = document.querySelector(SELECTORS.ZHIPIN.SEARCH.JOBLIST);
                const finalCount = finalJobUl ? finalJobUl.querySelectorAll(SELECTORS.ZHIPIN.SEARCH.JOBHREFS).length : 0;
                logger.add(`预加载完成，共加载 ${finalCount} 个岗位，开始提取职位详情`);
            };

            const pickNextKeyword = () => {
                if (!this.tags || !this.tags.length) {
                    throw new Error('未获取到岗位关键词列表');
                }
                currentTagIdx = (currentTagIdx + 1) % this.tags.length;
                currentKeyword = this.tags[currentTagIdx];
                return currentKeyword;
            };

            const startRound = async () => {
                resetRoundState();
                currentRound += 1;
                const keyword = pickNextKeyword();
                logger.divider();
                logger.add(`开始第 ${currentRound} 轮`);
                logger.add(`本轮搜索关键词：${keyword}`);
                window.scrollTo({ top: 0, left: 0, behavior: 'smooth' });
                await tools.asyncSleep(600);
                await search(keyword);
                logger.add(`第 ${currentRound} 轮已完成搜索（关键词：${keyword}），请在 ${(OPTIONS.manualFilterWaitMs / 1000).toFixed(0)} 秒内手动选择地区、薪资等筛选条件`);
                await tools.asyncSleep(OPTIONS.manualFilterWaitMs);
                if (activeMode === 'research') {
                    try {
                        currentResearchProject = await api.createResearchProject(keyword, location.href);
                        currentResearchStats = {
                            keyword,
                            candidateId: currentResearchProject.id,
                            scanned: 0,
                            created: 0,
                            duplicate: 0,
                            incomplete: 0,
                            failed: 0,
                        };
                        logger.add(`已创建调研项目：${currentResearchProject.id}`);
                    } catch (e) {
                        this.pause = true;
                        logger.add(`创建调研项目失败，采集已停止：${e}`);
                        logger.complete('创建失败');
                        return;
                    }
                }
                await preloadJobs();
                logger.add(`第 ${currentRound} 轮开始按当前筛选条件扫描岗位（关键词：${keyword}）`);
                loop();
            };

            // 主函数
            const main = async () => {
                logger.lockSettings();
                const clientConfig = await clientConfigPromise;
                const selectedKeywords = logger.getKeywords();
                if (!selectedKeywords.length) {
                    this.pause = true;
                    logger.resetStart();
                    return;
                }
                started = true;
                this.tags = selectedKeywords;
                localStorage.setItem(SEARCH_KEYWORDS_STORAGE_KEY, this.tags.join('\n'));
                activeMode = logger.getMode();
                activePreloadMaxRounds = logger.getPreloadMaxRounds();
                localStorage.setItem(PRELOAD_MAX_ROUNDS_STORAGE_KEY, String(activePreloadMaxRounds));
                OPTIONS.preloadMaxRounds = activePreloadMaxRounds;
                logger.add(`--程序启动：${activeMode === 'research' ? '调研模式' : '投递模式'}--`);
                logger.add('本轮搜索岗位：' + this.tags.join('、'));
                logger.add(`每个关键词最多预加载 ${activePreloadMaxRounds} 轮`);
                // 开始广播
                startBroadcast();
                if (typeof tagIdx === 'number' && this.tags.length) {
                    currentTagIdx = ((tagIdx % this.tags.length) + this.tags.length) % this.tags.length - 1;
                }
                if (activeMode === 'delivery') {
                    if (clientConfig && typeof clientConfig.introduce === 'string' && clientConfig.introduce) {
                        this.introduce = clientConfig.introduce;
                        logger.add('获取自我介绍成功');
                    } else {
                        this.introduce = await api.getIntroduce();
                        logger.add('获取自我介绍成功(旧接口)');
                    }
                }
                await startRound();
            };

            // 初始化
            const init = () => {
                if (start - tools.getTimestamp(this.targets.search) < OPTIONS.timestampTimeout) {
                    logger.add('搜索页已打开，等待启动');
                }
            };

            init();
        }

        // 详情页
        __detail() {
            // 注册广播
            const startBroadcast = () => {
                this.__broadcast(this.targets.detail);
            };
            startBroadcast();

            const buildUnavailableJobInfo = (reason) => ({
                title: '',
                company: '',
                salary: '',
                detail: '',
                actionText: '',
                chatUrl: '',
                addUrl: '',
                skip: true,
                skipReason: reason,
                talked: false,
            });

            // 获取职位信息
            const getJobInfo = () => {
                try {
                    const chatBtn = safeQuery(document, SELECTORS.ZHIPIN.DETAIL.STARTCHAT_CANDIDATES);
                    const title = safeText(document, SELECTORS.ZHIPIN.DETAIL.JOBNAME_CANDIDATES);
                    const salary = safeText(document, SELECTORS.ZHIPIN.DETAIL.SALARY_CANDIDATES);
                    const detail = safeText(document, SELECTORS.ZHIPIN.DETAIL.DESCRIPTION_CANDIDATES);
                    const company = safeText(document, SELECTORS.ZHIPIN.DETAIL.COMPANY_CANDIDATES);
                    const actionText = safeElementText(chatBtn);
                    const chatUrl = safeAttribute(chatBtn, SELECTORS.ZHIPIN.DETAIL.CHATURL);
                    const addUrl = safeAttribute(chatBtn, 'data-url');
                    const missingFields = [
                        ['职位名称', title],
                        ['公司', company],
                        ['薪资', salary],
                        ['职位描述', detail],
                    ].filter(([, value]) => !value).map(([label]) => label);
                    let skip = false;
                    let skipReason = '';

                    if (missingFields.length) {
                        skip = true;
                        skipReason = `页面结构变化或内容未加载，缺少：${missingFields.join('、')}`;
                    } else if (!chatBtn) {
                        skip = true;
                        skipReason = '未找到立即沟通按钮';
                    } else if (actionText.indexOf('立即沟通') === -1) {
                        skip = true;
                        skipReason = `按钮为 [${actionText || '未知'}]，疑似网申岗位`;
                    } else if (!chatUrl || !addUrl) {
                        skip = true;
                        skipReason = '缺少聊天链接，疑似异常岗位';
                    }

                    return {
                        title,
                        company,
                        salary,
                        detail,
                        actionText,
                        chatUrl,
                        addUrl,
                        skip,
                        skipReason,
                        talked: safeAttribute(chatBtn, 'data-isfriend') === 'true',
                    };
                } catch (e) {
                    return buildUnavailableJobInfo('页面结构变化，未能读取职位信息');
                }
            };
            const jobInfo = getJobInfo();

            // 来自搜索页
            const fromSearchPage = () => {
                // 把职位信息发送给搜索页
                this.broadcast.send(this.targets.search, this.bcTypes.GET_JOB_INFO, jobInfo);
            };

            // 来自聊天页
            const fromChatPage = () => {
                // 把职位信息发送给聊天页
                this.broadcast.send(
                    this.targets.chat,
                    this.bcTypes.GET_JOB_INFO,
                    jobInfo
                ).then(() => {
                    window.close();
                });
            };

            // 主函数
            const main = () => {
                // 判断来源
                const now = new Date().getTime();
                const isFromSearch = now - tools.getTimestamp(this.targets.detail) < OPTIONS.timestampTimeout && window.name === this.targets.detail;
                const isFromChat = now - tools.getTimestamp(this.targets.chat) < OPTIONS.timestampTimeout;

                if (isFromSearch) {
                    fromSearchPage();
                } else if (isFromChat) {
                    fromChatPage();
                }
            };
            main();
        }

        // 聊天页
        async __chat() {
            // 注册广播
            const startBroadcast = (target = this.targets.chat) => {
                this.__broadcast(target);
            };

            // 发送消息
            const sendMsg = (text) => {
                return new Promise(async (resolve, reject) => {
                    try {
                        const ipt = await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.CHATINPUT);
                        ipt.innerText = text;
                        await tools.asyncSleep(600);
                        const btn = await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.MSGSEND);
                        btn.click();
                        resolve();
                    } catch (e) {
                        reject();
                    }
                })
            };

            // 打招呼
            const sayHi = async () => {
                startBroadcast(this.targets.chatGreet);

                // 心跳 
                let count = 0;
                const loop = () => {
                    this.broadcast.sendAndReceive(
                        this.targets.search,
                        this.bcTypes.HEART_BEAT,
                        { count: ++count }
                    ).then((res) => {
                        if (res.success) {
                            setTimeout(loop, 1000);
                        } else {
                            throw new Error('心跳失联');
                        }
                    });
                };
                loop();

                try {
                    const greetDecision = await this.broadcast.sendAndReceive(this.targets.search, this.bcTypes.SAY_HI);
                    const introduce = greetDecision.introduce;
                    await sendMsg(introduce);
                    await logAction({
                        action: 'greet_message_sent',
                        scene: 'chat_greet',
                        resumeIndex: greetDecision.resumeIndex ?? OPTIONS.resumeIndex,
                    });
                    this.broadcast.send(this.targets.search, this.bcTypes.SAY_HI, { success: true }).then(() => {
                        this.broadcast.destroy();
                    });
                } catch (e) {
                    await logAction({
                        action: 'greet_message_failed',
                        scene: 'chat_greet',
                        reason: String(e),
                    });
                    this.broadcast.send(this.targets.search, this.bcTypes.SAY_HI, { success: false }).then(() => {
                        this.broadcast.destroy();
                    });
                }
            };

            // 获取聊天记录信息
            const getChatInfo = async () => {
                const ctn = await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.HISTORYCTN);

                const getMsgs = async () => {
                    const lis = Array.from(ctn.querySelectorAll(SELECTORS.ZHIPIN.CHAT.USEFULMSG));
                    // 提取历史记录
                    const msgs = [];
                    lis.forEach(li => {
                        const role = li.classList.contains('item-friend') ? 'user' : 'assistant';
                        const msgBox = li.querySelector(SELECTORS.ZHIPIN.CHAT.MSGCONTENT);
                        if (!msgBox) return;
                        msgs.push({
                            role,
                            content: msgBox.innerText,
                        });
                    });
                    // 提取简历，作品集状态
                    let needResume = 0;
                    let needWorks = 0;
                    let resumeSended = false;
                    let worksSended = false;
                    let confirmAddr = false;
                    // 判断聊天字眼中是否有相关信息
                    msgs.reverse();
                    let recent = '';
                    for (const msg of msgs) {
                        if (msg.role !== 'user') {
                            break;
                        }
                        recent += msg.content;
                    }
                    msgs.reverse();
                    if (recent.indexOf('简历') !== -1) {
                        needResume = 1;
                    }
                    if (recent.indexOf('作品') !== -1) {
                        needWorks = 1;
                    }
                    // 判断是否有过明确弹窗
                    const rlis = lis.reverse();
                    for (const li of rlis) {
                        if (li.classList.contains('item-myself')) {
                            break;
                        }
                        const bossGreen = li.querySelector('.boss-green');
                        const dialog = li.querySelector('.item-dialog');
                        if (bossGreen) {
                            const t = bossGreen.innerText;
                            if (t.indexOf('我想要一份您的附件简历，您是否同意\n拒绝\n同意') !== -1) {
                                needResume = 2;
                            }
                        } else if (dialog) {
                            const t = dialog.querySelector('.msg-dialog-title').innerText;
                            if (t.indexOf('您是否接受此工作地点?') !== -1) {
                                confirmAddr = true;
                            }
                        }
                    }
                    // 判断是否发过简历
                    const bossGreen = ctn.querySelectorAll('.boss-green');
                    if (bossGreen.length) {
                        bossGreen.forEach(el => {
                            const t = el.innerText;
                            if (t.indexOf('点击预览附件简历') !== -1) {
                                resumeSended = true;
                            }
                        });
                    }
                    return {
                        msgs,
                        needResume,
                        needWorks,
                        resumeSended,
                        worksSended,
                        confirmAddr,
                        talked: !msgs.every(d => d.role === 'user'),
                        jobEl: (await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.JOBEL)).querySelector(SELECTORS.ZHIPIN.CHAT.JOBCITY)
                    };
                };

                const scroll2Top = async () => {
                    if (ctn.scrollTop === 0) return;
                    ctn.scrollTop = 0;
                    await tools.asyncSleep(300);
                    await scroll2Top();
                };

                // 滚动到顶部
                await tools.asyncSleep(300);
                await scroll2Top();
                // 获取聊天记录
                return await getMsgs();
            };

            // 发送简历
            const sendResume = async (resumeIndex = OPTIONS.resumeIndex) => {
                const sendBtn = await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.RESUMESEND);
                sendBtn.click();

                // 可能是弹一个小窗
                const smallDialog = await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.RESUMEMODAL).catch(() => null);
                if (smallDialog) {
                    smallDialog.querySelector(SELECTORS.ZHIPIN.CHAT.RESUMEMODALCONFIRM).click();
                    await sendMsg('已发送，请查收');
                    return {
                        mode: 'small_dialog',
                        selectedResumeIndex: resumeIndex,
                    };
                }

                // 弹出大窗让选择
                const resumeCtn = await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.RESUMELIST);
                const confirm = await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.RESUMESENDCONFIRM);
                const resumes = resumeCtn.querySelectorAll(SELECTORS.ZHIPIN.CHAT.RESUMELISTITEM);
                const fallbackIndex = resumes[resumeIndex] ? resumeIndex : (resumes[OPTIONS.resumeIndex] ? OPTIONS.resumeIndex : 0);
                const resume = resumes[fallbackIndex];
                await tools.asyncSleep(300);
                resume.click();
                await tools.asyncSleep(300);
                confirm.click();
                await sendMsg('已发送，请查收');
                return {
                    mode: 'resume_list',
                    selectedResumeIndex: fallbackIndex,
                };
            };

            // 发送作品集
            const sendWorks = async () => {
                logger.add('sendWks');
            };

            let logger = null;
            // 给搜索页同步状态
            const status = (text) => {
                logger && logger.add(text);
                this.broadcast && this.broadcast.send(
                    this.targets.search,
                    this.bcTypes.STATUS,
                    text
                );
            };
            // 分割线
            const divider = () => {
                logger && logger.divider();
                this.broadcast && this.broadcast.send(this.targets.search, this.bcTypes.DIVIDER);
            };

            // 聊天
            const chat = async () => {
                // api
                const api = new Api();
                const logAction = async (payload) => {
                    try {
                        await api.logAction(payload);
                    } catch (e) {
                        console.log('logAction failed', e);
                    }
                };
                // 开始广播
                startBroadcast(this.targets.chat);
                // 获取默认自我介绍（兜底）
                const defaultIntroduce = (await this.broadcast.sendAndReceive(
                    this.targets.search,
                    this.bcTypes.INTRODUCE,
                )).introduce;
                // 心跳
                let count = 0;
                const loop = async () => {
                    await this.broadcast.sendAndReceive(
                        this.targets.search,
                        this.bcTypes.HEART_BEAT,
                        { count: ++count }
                    ).then((res) => {
                        if (res.success) {
                            setTimeout(loop, 1000);
                        } else {
                            throw new Error('心跳失联');
                        }
                    });
                };
                loop();

                // 一轮
                let round = 0;
                let lastTop = 0;
                const once = async () => {
                    // 获取联系人列表
                    let empty = false;
                    const ctn = await tools.endlessFind(SELECTORS.ZHIPIN.CHAT.CONTACTLIST).catch(e => {
                        if (document.querySelector(SELECTORS.ZHIPIN.CHAT.CONTACTLISTEMPTY)) {
                            status('当前暂无消息');
                            empty = true;
                        }
                    });
                    if (empty) return;
                    const lis = ctn.querySelectorAll(SELECTORS.ZHIPIN.CHAT.CONTACTLISTITEM);
                    // 遍历新消息
                    for (const ls of lis) {
                        try {
                            // 无新消息
                            if (!ls.querySelector(SELECTORS.ZHIPIN.CHAT.NEWMSGNOTICE)) continue;
                            // 获取联系人信息
                            const name = ls.querySelector(SELECTORS.ZHIPIN.CHAT.USERNAME);
                            const company = name.nextElementSibling.innerText;
                            divider();
                            status(`[${company} - ${name.innerText}] 发来一条新消息`);
                            // 进入聊天界面
                            name.click();
                            // 获取聊天记录信息
                            const chatInfo = await getChatInfo();
                            // 如果最新的是我的回复
                            const lastMsg = chatInfo.msgs.slice(-1)[0];
                            if (lastMsg && lastMsg.role === 'assistant') continue;
                            // 如果以前没聊过
                            if (!chatInfo.talked) {
                                localStorage.setItem(this.targets.chat, new Date().getTime());
                                chatInfo.jobEl.click();
                                status(`正在获取职位详情`);
                                const jobInfo = await this.broadcast.receive(this.targets.detail, this.bcTypes.GET_JOB_INFO);
                                // 获取职位匹配度
                                status(`开始计算职位 [${jobInfo.title}] 的匹配度`);
                                const decision = await api.getJobScore(jobInfo.title, jobInfo.salary, jobInfo.detail);
                                status(`匹配度: ${decision.score} | 简历索引: ${decision.resumeIndex}`);
                                await logAction({
                                    action: 'job_decision_consumed',
                                    scene: 'chat',
                                    title: jobInfo.title,
                                    salary: jobInfo.salary,
                                    score: decision.score,
                                    resumeIndex: decision.resumeIndex,
                                });
                                // 如果分数达到阈值并且未聊过天，打个招呼
                                if (decision.score >= OPTIONS.thread && !chatInfo.msgs.length) {
                                    status(`正在给职位 [${jobInfo.title}] 发送打招呼消息`);
                                    try {
                                        await sendMsg(decision.introduce || defaultIntroduce);
                                        await logAction({
                                            action: 'chat_greet_sent',
                                            scene: 'chat',
                                            title: jobInfo.title,
                                            resumeIndex: decision.resumeIndex,
                                        });
                                        status(`打招呼成功`);
                                    } catch (e) {
                                        await logAction({
                                            action: 'chat_greet_failed',
                                            scene: 'chat',
                                            title: jobInfo.title,
                                            resumeIndex: decision.resumeIndex,
                                            reason: String(e),
                                        });
                                        status(`打招呼失败: ${e}`);
                                    }
                                    continue;
                                }
                                // 未达到阈值，直接下一个
                                else if (decision.score < OPTIONS.thread) {
                                    await logAction({
                                        action: 'chat_rejected_below_threshold',
                                        scene: 'chat',
                                        title: jobInfo.title,
                                        score: decision.score,
                                        threshold: OPTIONS.thread,
                                        resumeIndex: decision.resumeIndex,
                                    });
                                    await sendMsg('不好意思，不太合适哈，祝早日找到合适的人选。')
                                    continue;
                                }
                            }
                            let isChat = true;
                            // 只要对方发来新消息且还没发过简历，就直接发送简历，不再调用大模型聊天
                            if (!chatInfo.resumeSended) {
                                isChat = false;
                                localStorage.setItem(this.targets.chat, new Date().getTime());
                                chatInfo.jobEl.click();
                                status(`正在获取职位详情（用于确定简历）`);
                                const jobInfo = await this.broadcast.receive(this.targets.detail, this.bcTypes.GET_JOB_INFO);
                                const decision = await api.getJobScore(jobInfo.title, jobInfo.salary, jobInfo.detail);
                                status(`检测到新消息，直接发送简历（简历索引 ${decision.resumeIndex}）`);
                                const resumeResult = await sendResume(decision.resumeIndex);
                                await logAction({
                                    action: 'resume_sent',
                                    scene: 'chat',
                                    title: jobInfo.title,
                                    salary: jobInfo.salary,
                                    requestedResumeIndex: decision.resumeIndex,
                                    selectedResumeIndex: resumeResult?.selectedResumeIndex ?? decision.resumeIndex,
                                    sendMode: resumeResult?.mode || 'unknown',
                                });
                                status('发送成功');
                            }
                            // 是否需要作品集（当前关闭自动发送，仅保留原入口）
                            if (chatInfo.needWorks && !chatInfo.worksSended) {
                                isChat = false;
                                status('检测到作品集相关消息，当前未开启自动发送作品集');
                            }
                            // 聊天
                            if (isChat) {
                                status('已发过简历，跳过自动聊天');
                            }
                        } catch (e) {
                            status('回复某条消息出错');
                        }
                    }
                    // 向下滚动
                    ctn.scrollTop = 1014 * ++round;
                    await tools.asyncSleep(300);
                    if (ctn.scrollTop !== lastTop) {
                        lastTop = ctn.scrollTop;
                        await once();
                    }
                };
                // 完成一轮
                await once();
            };

            // 主函数
            const main = async () => {
                // 判断来源
                const now = new Date().getTime();
                const isGreet = now - tools.getTimestamp(this.targets.chatGreet) < OPTIONS.timestampTimeout && window.name === this.targets.chatGreet;
                const isChat = now - tools.getTimestamp(this.targets.chat) < OPTIONS.timestampTimeout && window.name === this.targets.chat;

                if (isGreet) {
                    sayHi();
                }
                else if (isChat) {
                    // 日志
                    logger = new Logger();
                    logger.runBtn.remove();
                    logger.clearBtn.remove();
                    // 等待加载
                    await tools.asyncSleep(3000);
                    chat()
                        .then(async () => {
                            status('消息处理完毕');
                            await this.broadcast.send(this.targets.search, this.bcTypes.RUN, true);
                        })
                        .catch(async () => {
                            status('聊天程序运行出错');
                            await this.broadcast.send(this.targets.search, this.bcTypes.RUN, false);
                        }).finally(() => {
                            this.broadcast.destroy();
                        });
                }
            };
            main();
        }

        // 运行
        run(tagIdx = 0) {
            const path = location.pathname;
            // 在搜索页
            if (path.startsWith(SEARCHPATH.zhipin)) {
                this.__search(tagIdx);
            }
            // 在详情页
            else if (path.startsWith(this.whiteList.deatil)) {
                this.__detail();
            }
            // 在聊天页
            else if (path.startsWith(this.whiteList.chat)) {
                this.__chat();
            }
            // 否则跳转搜索页
            else {
                const navigationLogger = new Logger(() => {
                    tools.openTabNSetTimestamp(SEARCHPATH.zhipin, this.targets.search, true);
                });
                navigationLogger.runBtn.innerText = '打开搜索页';
            }
        }
    }

    const goodjobs = new Zhipin().run();
})();
