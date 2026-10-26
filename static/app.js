(() => {
  const SECTIONS = ["single", "multiple", "true_false", "fill_blank"];
  const LABELS = { single: "单项选择题", multiple: "不定项选择题", true_false: "判断题", fill_blank: "填空题" };
  const LETTERS = { single: "一", multiple: "二", true_false: "三", fill_blank: "四" };
  const state = {
    user: null, csrf: "", view: "dashboard", dashboard: null, bank: null,
    practice: { section: null, question: null, result: null, answer: null },
    exam: null, examIndex: 0, examSaveTimer: null, examSaveState: "已保存", timer: null,
    collection: { kind: "wrong", questions: [], page: 1, hasMore: false },
    history: [], adminTab: "settings", adminConfig: null, adminQuestions: [], adminPage: 1,
    adminSearch: "", adminSection: "", users: [], audit: [], importPreview: null,
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const fmtDate = (stamp) => stamp ? new Date(stamp * 1000).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" }) : "—";
  const fmtDuration = (seconds) => {
    const safe = Math.max(0, Math.floor(seconds));
    return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
  };
  const sectionOptions = (selected = "") => SECTIONS.map((key) => `<option value="${key}" ${selected === key ? "selected" : ""}>${LABELS[key]}</option>`).join("");

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !(options.body instanceof Blob) && !(options.body instanceof FormData) && typeof options.body !== "string") {
      headers.set("Content-Type", "application/json");
      options.body = JSON.stringify(options.body);
    }
    if (state.csrf && options.method && options.method !== "GET") headers.set("X-CSRF-Token", state.csrf);
    const response = await fetch(path, { credentials: "same-origin", ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || `请求失败（${response.status}）`);
      error.data = data;
      throw error;
    }
    return data;
  }

  let toastTimer;
  function toast(message, kind = "") {
    const node = $("#toast");
    node.textContent = message;
    node.className = `toast visible ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.className = "toast"; }, 2800);
  }

  function setView(view) {
    state.view = view;
    $$("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
    const main = $("#mainContent");
    main.innerHTML = `<div class="loading-card"><span class="spinner"></span><span>正在加载…</span></div>`;
    if (view === "dashboard") loadDashboard();
    else if (view === "practice") renderPracticeHome();
    else if (view === "exam") loadExamView();
    else if (view === "wrong" || view === "favorites") loadCollection(view === "wrong" ? "wrong" : "favorites");
    else if (view === "history") loadHistory();
    else if (view === "admin") renderAdmin();
  }

  async function boot() {
    try {
      const data = await api("/api/me");
      state.user = data.user;
      state.csrf = data.csrf_token;
      showApp();
      setView("dashboard");
    } catch (_) {
      showLogin();
    }
  }

  function showLogin() {
    $("#loginScreen").hidden = false;
    $("#appShell").hidden = true;
    $("#loginUsername").focus();
  }

  function showApp() {
    $("#loginScreen").hidden = true;
    $("#appShell").hidden = false;
    $("#userBadge").textContent = `${state.user.display_name} · ${state.user.role === "admin" ? "管理员" : "学生"}`;
    $("#adminNavGroup").hidden = state.user.role !== "admin";
  }

  async function loadDashboard() {
    try {
      const [dashboard, bank, progress] = await Promise.all([api("/api/dashboard"), api("/api/bank"), api("/api/practice/progress")]);
      state.dashboard = dashboard;
      state.bank = bank;
      const sectionCards = SECTIONS.map((section) => {
        const item = progress.sections[section] || { total: 0, done: 0, wrong: 0 };
        const percent = item.total ? Math.round(item.done * 100 / item.total) : 0;
        return `<article class="section-progress"><div class="section-progress-top"><span class="section-mark">${LETTERS[section]}</span><div><strong>${LABELS[section]}</strong><small>${item.done} / ${item.total} 已完成</small></div></div><div class="progress-track"><span style="width:${percent}%"></span></div><button class="link-button" data-practice-section="${section}">继续练习 <span>→</span></button></article>`;
      }).join("");
      const percent = dashboard.completion_percent || 0;
      $("#mainContent").innerHTML = `
        <section class="welcome-row"><div><p class="eyebrow">学习空间</p><h1>你好，${esc(state.user.display_name)}</h1><p class="muted">按原题库顺序练习，或者开始一场计时模拟考试。</p></div><div class="welcome-date">${new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" })}</div></section>
        <section class="stat-grid">
          <article class="stat-card"><span class="stat-icon indigo">题</span><div><small>题库总量</small><strong>${dashboard.total_questions.toLocaleString()}</strong><span>四类题型</span></div></article>
          <article class="stat-card"><span class="stat-icon teal">✓</span><div><small>顺序练习进度</small><strong>${percent}%</strong><span>${dashboard.completed_questions} 道已作答</span></div></article>
          <article class="stat-card"><span class="stat-icon amber">↻</span><div><small>待复习错题</small><strong>${dashboard.wrong_count}</strong><span>继续订正巩固</span></div></article>
          <article class="stat-card"><span class="stat-icon rose">◷</span><div><small>已完成模拟考试</small><strong>${dashboard.exam_count}</strong><span>个人练习记录</span></div></article>
        </section>
        <section class="content-grid"><div class="panel"><div class="panel-heading"><div><p class="eyebrow">循序渐进</p><h2>按题型继续练习</h2></div><button class="text-button" data-view="practice">全部题型 →</button></div><div class="section-progress-list">${sectionCards}</div></div>
          <aside class="panel exam-promo"><div class="promo-orbit">◷</div><p class="eyebrow">限时自测</p><h2>准备好检验学习成果了吗？</h2><p class="muted">按照管理员设置的题型比例随机组卷，考试中不显示答案。</p><button class="button primary" data-view="exam">进入模拟考试 <span>→</span></button>${dashboard.last_exam ? `<div class="last-score"><span>上次模拟考试</span><strong>${dashboard.last_exam.score} <small>/ ${dashboard.last_exam.max_score}</small></strong></div>` : ""}</aside></section>
        <section class="panel bank-panel"><div class="panel-heading"><div><p class="eyebrow">题库目录</p><h2>题库内容</h2></div><span class="pill">依 PDF 顺序组织</span></div><div class="bank-counts">${SECTIONS.map((key) => `<div><span>${LABELS[key]}</span><strong>${bank.counts[key].toLocaleString()} <small>题</small></strong></div>`).join("")}</div></section>`;
    } catch (error) { showError(error); }
  }

  async function renderPracticeHome() {
    try {
      const [bank, progress] = await Promise.all([api("/api/bank"), api("/api/practice/progress")]);
      state.bank = bank;
      const cards = SECTIONS.map((section) => {
        const p = progress.sections[section];
        const percent = p.total ? Math.round(p.done * 100 / p.total) : 0;
        return `<article class="practice-type-card"><div class="type-card-top"><span class="section-mark">${LETTERS[section]}</span><span class="pill">${p.total} 题</span></div><h2>${LABELS[section]}</h2><p class="muted">完成 ${p.done} 题${p.wrong ? ` · ${p.wrong} 题曾答错` : " · 按题号顺序推进"}</p><div class="progress-track"><span style="width:${percent}%"></span></div><button class="button ${p.done === p.total && p.total ? "quiet" : "primary"}" data-practice-section="${section}">${p.done === p.total && p.total ? "重新复习" : p.done ? "继续练习" : "开始练习"}<span>→</span></button></article>`;
      }).join("");
      $("#mainContent").innerHTML = `<section class="page-heading"><div><p class="eyebrow">顺序刷题</p><h1>从题库原序开始，逐题巩固</h1><p class="muted">答完立即显示正确答案。练习进度会自动保存，完成后可重新复习。</p></div></section><section class="practice-type-grid">${cards}</section>${state.practice.question ? practiceQuestionMarkup() : `<section class="hint-panel"><span class="hint-symbol">✓</span><div><strong>每道题答完都会立即反馈</strong><p>未作答的题不会标记为已完成；再次进入时会从最早未完成题目继续。</p></div></section>`}`;
    } catch (error) { showError(error); }
  }

  async function startPractice(section) {
    state.practice.section = section;
    state.practice.result = null;
    state.practice.answer = null;
    await loadPracticeQuestion();
  }

  async function loadPracticeQuestion(number = "") {
    if (!state.practice.section) return renderPracticeHome();
    try {
      const query = new URLSearchParams({ section: state.practice.section });
      if (number) query.set("number", number);
      const data = await api(`/api/practice/question?${query}`);
      state.practice.question = data.question;
      state.practice.result = null;
      state.practice.answer = null;
      renderPracticeView();
    } catch (error) { showError(error); }
  }

  function renderPracticeView() {
    const q = state.practice.question;
    if (!q) return renderPracticeHome();
    $("#mainContent").innerHTML = `<section class="page-heading compact-heading"><div><button class="back-link" data-action="practice-home">← 所有题型</button><p class="eyebrow">${LABELS[q.section]} · 原题号 ${q.number}</p><h1>顺序刷题</h1></div><button class="button quiet" data-action="practice-next">下一题 <span>→</span></button></section>${q.completed ? `<div class="notice success-notice">这一类题目已全部完成，现在从第一题开始复习。</div>` : ""}${practiceQuestionMarkup()}`;
  }

  function practiceQuestionMarkup() {
    const q = state.practice.question;
    if (!q) return "";
    const result = state.practice.result;
    return `<article class="question-card"><div class="question-meta"><span class="pill">${LABELS[q.section]}</span><span>第 ${q.number} 题</span><span>PDF 第 ${q.source_page} 页</span><button class="favorite-button ${q.favorite ? "favorited" : ""}" data-action="favorite" data-question="${q.id}" aria-label="收藏题目">${q.favorite ? "★ 已收藏" : "☆ 收藏"}</button></div><div class="question-stem">${esc(q.stem)}</div>${answerControls(q, state.practice.answer, Boolean(result))}${result ? `<div class="answer-feedback ${result.correct ? "is-correct" : "is-wrong"}"><div class="feedback-title">${result.correct ? "回答正确" : "再记一下正确答案"}</div><p>正确答案：<strong>${esc(formatAnswer(q.section, result.correct_answer))}</strong></p>${result.explanation ? `<p class="explanation"><span>解析</span>${esc(result.explanation)}</p>` : `<p class="muted">本题暂未提供解析。</p>`}</div>` : `<div class="question-actions"><span class="muted">提交后立即显示答案</span><button class="button primary" data-action="submit-practice" ${state.practice.answer == null || state.practice.answer === "" || (Array.isArray(state.practice.answer) && !state.practice.answer.length) ? "disabled" : ""}>提交答案</button></div>`}</article>`;
  }

  function answerControls(q, current, disabled = false) {
    if (q.section === "single" || q.section === "true_false") {
      const values = q.section === "true_false" ? [{ key: "√", text: "正确" }, { key: "×", text: "错误" }] : q.options;
      return `<div class="choice-list ${q.section === "true_false" ? "boolean-list" : ""}">${values.map((option) => `<label class="choice-option ${current === option.key ? "selected" : ""}"><input type="radio" name="answerChoice" value="${esc(option.key)}" ${current === option.key ? "checked" : ""} ${disabled ? "disabled" : ""}><span class="choice-key">${esc(option.key)}</span><span>${esc(option.text)}</span></label>`).join("")}</div>`;
    }
    if (q.section === "multiple") {
      const selected = Array.isArray(current) ? current : [];
      return `<div class="choice-list">${q.options.map((option) => `<label class="choice-option ${selected.includes(option.key) ? "selected" : ""}"><input type="checkbox" name="answerChoice" value="${esc(option.key)}" ${selected.includes(option.key) ? "checked" : ""} ${disabled ? "disabled" : ""}><span class="choice-key">${esc(option.key)}</span><span>${esc(option.text)}</span></label>`).join("")}</div>`;
    }
    return `<label class="fill-answer"><span>填写答案</span><textarea id="fillAnswer" rows="3" placeholder="多个空请按顺序填写，并用逗号分隔" ${disabled ? "disabled" : ""}>${esc(Array.isArray(current) ? current.join(", ") : current || "")}</textarea><small>若有多个空，请按题目顺序填写。</small></label>`;
  }

  function readPracticeAnswer() {
    const q = state.practice.question;
    if (q.section === "single" || q.section === "true_false") return $("input[name=answerChoice]:checked")?.value ?? null;
    if (q.section === "multiple") return $$("input[name=answerChoice]:checked").map((input) => input.value);
    return $("#fillAnswer")?.value.trim() ?? "";
  }

  async function submitPractice() {
    const q = state.practice.question;
    const answer = readPracticeAnswer();
    state.practice.answer = answer;
    if (answer == null || answer === "" || (Array.isArray(answer) && answer.length === 0)) return toast("请先作答。", "error");
    try {
      state.practice.result = await api("/api/practice/answer", { method: "POST", body: { question_id: q.id, answer } });
      renderPracticeView();
      if (state.practice.result.correct) toast("回答正确，进度已保存。", "success");
      else toast("已记录错题，正确答案已显示。");
    } catch (error) { toast(error.message, "error"); }
  }

  function formatAnswer(section, answer) {
    if (Array.isArray(answer)) return answer.join("、");
    if (section === "true_false") return answer === "√" || answer === "对" ? "正确（√）" : answer === "×" || answer === "错" ? "错误（×）" : answer;
    return String(answer ?? "");
  }

  async function toggleFavorite(questionId) {
    try {
      const data = await api("/api/favorites/toggle", { method: "POST", body: { question_id: Number(questionId) } });
      if (state.practice.question?.id === Number(questionId)) state.practice.question.favorite = data.favorite;
      if (state.view === "favorites" && !data.favorite) loadCollection("favorites");
      else if (state.view === "practice") renderPracticeView();
      toast(data.favorite ? "已加入收藏。" : "已取消收藏。", "success");
    } catch (error) { toast(error.message, "error"); }
  }

  async function startExam() {
    try {
      const data = await api("/api/exams/start", { method: "POST", body: {} });
      if (data.result) renderExamResult(data.result, "已自动提交超时考试。");
      else { state.exam = data.exam; state.examIndex = 0; renderExam(); }
    } catch (error) { toast(error.message, "error"); }
  }

  function renderExam() {
    stopTimer();
    const exam = state.exam;
    if (!exam) return loadExamView();
    const current = exam.items[state.examIndex] || exam.items[0];
    state.examIndex = Math.max(0, exam.items.indexOf(current));
    const answered = exam.items.filter((item) => hasAnswer(item.user_answer)).length;
    const remaining = exam.ends_at - Date.now() / 1000;
    if (remaining <= 0) { submitExam(true); return; }
    $("#mainContent").innerHTML = `<section class="exam-topline"><div><p class="eyebrow">${esc(exam.config.name)}</p><h1>模拟考试</h1></div><div class="exam-clock"><small>剩余时间</small><strong id="examCountdown">${fmtDuration(remaining)}</strong></div></section><div class="exam-layout"><section class="exam-main"><div class="exam-progress-row"><span>第 ${state.examIndex + 1} / ${exam.items.length} 题</span><span id="examSaveState" class="save-state">${esc(state.examSaveState)}</span></div><div class="exam-progress-track"><span style="width:${Math.round(answered * 100 / exam.items.length)}%"></span></div><article class="question-card exam-question-card"><div class="question-meta"><span class="pill">${LABELS[current.section]}</span><span>题库原题号 ${current.number}</span><span>PDF 第 ${current.source_page} 页</span></div><div class="question-stem">${esc(current.stem)}</div>${answerControls(current, current.user_answer, false)}<div class="question-actions"><span class="muted">当前答题：${answered} / ${exam.items.length}</span><div class="inline-actions"><button class="button quiet" data-action="exam-prev" ${state.examIndex === 0 ? "disabled" : ""}>上一题</button><button class="button quiet" data-action="exam-next" ${state.examIndex === exam.items.length - 1 ? "disabled" : ""}>下一题</button></div></div></article></section><aside class="panel exam-answer-sheet"><div class="panel-heading"><div><p class="eyebrow">答题卡</p><h3>题目导航</h3></div><span class="answered-counter">${answered}/${exam.items.length}</span></div><div class="answer-sheet-grid">${exam.items.map((item, index) => `<button class="answer-sheet-cell ${hasAnswer(item.user_answer) ? "answered" : ""} ${index === state.examIndex ? "current" : ""}" data-exam-index="${index}" aria-label="第 ${index + 1} 题">${index + 1}</button>`).join("")}</div><div class="sheet-legend"><span><i class="legend-dot current-dot"></i>当前题</span><span><i class="legend-dot answered-dot"></i>已作答</span><span><i class="legend-dot"></i>未作答</span></div><div class="submit-exam-box"><p>还有 <strong>${exam.items.length - answered}</strong> 道题未作答</p><button class="button danger wide-button" data-action="submit-exam">交卷</button></div></aside></div>`;
    startTimer();
  }

  function hasAnswer(answer) { return answer != null && answer !== "" && !(Array.isArray(answer) && answer.length === 0); }

  function startTimer() {
    stopTimer();
    state.timer = setInterval(() => {
      if (!state.exam || state.view !== "exam") return stopTimer();
      const seconds = state.exam.ends_at - Date.now() / 1000;
      const node = $("#examCountdown");
      if (!node) return;
      node.textContent = fmtDuration(seconds);
      node.classList.toggle("urgent", seconds <= 300);
      if (seconds <= 0) submitExam(true);
    }, 1000);
  }
  function stopTimer() { if (state.timer) clearInterval(state.timer); state.timer = null; }

  function queueExamAnswer() {
    if (!state.exam) return;
    const current = state.exam.items[state.examIndex];
    current.user_answer = readExamAnswer(current);
    state.examSaveState = "保存中…";
    const label = $("#examSaveState");
    if (label) label.textContent = state.examSaveState;
    clearTimeout(state.examSaveTimer);
    state.examSaveTimer = setTimeout(async () => {
      try {
        await api("/api/exams/answer", { method: "POST", body: { exam_id: state.exam.id, question_id: current.id, answer: current.user_answer } });
        state.examSaveState = "已保存";
        const currentLabel = $("#examSaveState");
        if (currentLabel) currentLabel.textContent = state.examSaveState;
      } catch (error) {
        if (error.data?.result) renderExamResult(error.data.result, "考试时间结束，系统已自动交卷。");
        else { state.examSaveState = "保存失败"; const currentLabel = $("#examSaveState"); if (currentLabel) currentLabel.textContent = error.message; }
      }
    }, 180);
  }

  function readExamAnswer(q) {
    if (q.section === "single" || q.section === "true_false") return $("input[name=answerChoice]:checked")?.value ?? null;
    if (q.section === "multiple") return $$("input[name=answerChoice]:checked").map((input) => input.value);
    return $("#fillAnswer")?.value.trim() ?? "";
  }

  async function submitExam(automatic = false) {
    if (!state.exam) return;
    if (!automatic && !window.confirm("确定现在交卷吗？交卷后不能继续作答。")) return;
    clearTimeout(state.examSaveTimer);
    // Flush the current input before submitting, so the final change is included.
    const current = state.exam.items[state.examIndex];
    current.user_answer = readExamAnswer(current);
    try {
      await api("/api/exams/answer", { method: "POST", body: { exam_id: state.exam.id, question_id: current.id, answer: current.user_answer } }).catch(() => {});
      const data = await api("/api/exams/submit", { method: "POST", body: { exam_id: state.exam.id } });
      state.exam = null;
      stopTimer();
      renderExamResult(data.result, automatic ? "考试时间结束，系统已自动交卷。" : "考试已提交。");
    } catch (error) { toast(error.message, "error"); }
  }

  function renderExamResult(result, message = "") {
    stopTimer();
    state.exam = null;
    const items = result.items.map((item, index) => `<article class="review-question ${item.is_correct ? "review-correct" : "review-wrong"}"><div class="review-heading"><span class="result-number">${index + 1}</span><span class="pill">${LABELS[item.section]}</span><span>原题号 ${item.number}</span><strong class="review-mark">${item.is_correct ? "答对" : "答错"} · ${item.earned}/${item.points} 分</strong></div><div class="review-stem">${esc(item.stem)}</div>${item.options?.length ? `<div class="review-options">${item.options.map((o) => `<span>${esc(o.key)}. ${esc(o.text)}</span>`).join("")}</div>` : ""}<div class="review-answers"><span>你的答案：<strong>${esc(formatAnswer(item.section, item.user_answer) || "未作答")}</strong></span><span>正确答案：<strong>${esc(formatAnswer(item.section, item.correct_answer))}</strong></span></div>${item.explanation ? `<p class="explanation"><span>解析</span>${esc(item.explanation)}</p>` : ""}</article>`).join("");
    $("#mainContent").innerHTML = `<section class="result-hero"><div class="result-score-ring"><strong>${result.score}</strong><span>/ ${result.max_score}</span></div><div><p class="eyebrow">${esc(result.config.name)}</p><h1>本场考试已完成</h1><p>${esc(message)} · 正确率 ${result.accuracy}% · 共 ${result.items.length} 题</p></div><button class="button primary" data-view="exam">再考一次</button></section><section class="review-summary"><div><small>最终得分</small><strong>${result.score} <small>/ ${result.max_score}</small></strong></div><div><small>答对题目</small><strong>${result.items.filter((i) => i.is_correct).length} <small>/ ${result.items.length}</small></strong></div><div><small>正确率</small><strong>${result.accuracy}<small>%</small></strong></div><button class="text-button" data-view="history">查看考试记录 →</button></section><section class="review-list-heading"><div><p class="eyebrow">逐题复盘</p><h2>答案与解析</h2></div><button class="button quiet" data-view="dashboard">返回学习概览</button></section><div class="review-list">${items}</div>`;
  }

  async function loadCollection(kind, page = 1, append = false) {
    state.collection.kind = kind;
    try {
      const actual = await api(kind === "favorites" ? `/api/favorites?page=${page}` : `/api/practice/wrong?page=${page}`);
      if (!append) state.collection.questions = [];
      state.collection.questions.push(...actual.questions);
      state.collection.page = page;
      state.collection.hasMore = actual.has_more;
      renderCollection();
    } catch (error) { showError(error); }
  }

  function renderCollection() {
    const kind = state.collection.kind;
    const title = kind === "wrong" ? "错题本" : "我的收藏";
    const intro = kind === "wrong" ? "集中复习曾经答错的题目。" : "收藏的题目会保存在个人账号中。";
    const rows = state.collection.questions.map((q) => `<article class="collection-row"><div><div class="collection-meta"><span class="pill">${LABELS[q.section]}</span><span>第 ${q.number} 题</span><span>PDF 第 ${q.source_page} 页</span></div><p>${esc(q.stem)}</p></div><div class="collection-actions"><button class="button quiet" data-open-question="${q.section}" data-number="${q.number}">开始复习</button>${kind === "favorites" ? `<button class="icon-button remove-favorite" data-action="favorite" data-question="${q.id}" title="取消收藏">★</button>` : ""}</div></article>`).join("");
    $("#mainContent").innerHTML = `<section class="page-heading"><div><p class="eyebrow">个人学习</p><h1>${title}</h1><p class="muted">${intro}</p></div><span class="pill">${state.collection.questions.length} 道题</span></section><section class="panel collection-list">${rows || `<div class="empty-state"><span>${kind === "wrong" ? "↻" : "☆"}</span><h3>${kind === "wrong" ? "还没有错题" : "还没有收藏"}</h3><p>${kind === "wrong" ? "顺序练习后，答错的题会自动收录在这里。" : "练习或复习时点击“收藏”即可保存题目。"}</p></div>`}</section>${state.collection.hasMore ? `<div class="load-more-wrap"><button class="button quiet" data-action="collection-more">加载更多</button></div>` : ""}`;
  }

  async function loadHistory() {
    try {
      const data = await api("/api/exams/history");
      state.history = data.exams;
      const rows = data.exams.filter((item) => item.status === "submitted").map((item) => `<tr><td><strong>${esc(item.name)}</strong><small>${fmtDate(item.started_at)}</small></td><td>${item.score} / ${item.max_score}</td><td><span class="score-chip">已完成</span></td><td><button class="link-button" data-review="${item.id}">查看复盘 →</button></td></tr>`).join("");
      $("#mainContent").innerHTML = `<section class="page-heading"><div><p class="eyebrow">个人记录</p><h1>考试记录</h1><p class="muted">只展示当前账号的模拟考试成绩和逐题复盘。</p></div><button class="button primary" data-view="exam">开始模拟考试</button></section><section class="panel table-panel"><table><thead><tr><th>考试名称 / 时间</th><th>得分</th><th>状态</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="4"><div class="empty-state compact-empty"><h3>还没有完成的考试</h3><p>完成模拟考试后，成绩和复盘会显示在这里。</p></div></td></tr>`}</tbody></table></section>`;
    } catch (error) { showError(error); }
  }

  async function openReview(id) {
    try { const data = await api(`/api/exams/${id}`); renderExamResult(data.result); }
    catch (error) { toast(error.message, "error"); }
  }

  function adminTabs() {
    const tabs = [["settings", "考试参数"], ["questions", "题库维护"], ["import", "PDF 导入"], ["users", "学生账号"], ["audit", "操作记录"]];
    return `<div class="admin-tabs">${tabs.map(([key, label]) => `<button class="admin-tab ${state.adminTab === key ? "active" : ""}" data-admin-tab="${key}">${label}</button>`).join("")}</div>`;
  }

  async function renderAdmin() {
    if (state.user.role !== "admin") return toast("仅管理员可以访问此页面。", "error");
    $("#mainContent").innerHTML = `<section class="page-heading"><div><p class="eyebrow">平台管理</p><h1>管理员中心</h1><p class="muted">管理考试模板、题库内容和学生账号。</p></div></section>${adminTabs()}<div id="adminContent" class="admin-content"><div class="loading-card"><span class="spinner"></span><span>正在加载…</span></div></div>`;
    await loadAdminTab();
  }

  async function loadAdminTab() {
    const content = $("#adminContent");
    if (!content) return;
    content.innerHTML = `<div class="loading-card"><span class="spinner"></span><span>正在加载…</span></div>`;
    try {
      if (state.adminTab === "settings") {
        const data = await api("/api/admin/settings");
        state.adminConfig = data.config;
        content.innerHTML = adminSettingsMarkup(data.config, data.official_values_found);
      } else if (state.adminTab === "questions") {
        await loadAdminQuestions();
      } else if (state.adminTab === "users") {
        const data = await api("/api/admin/users"); state.users = data.users; content.innerHTML = adminUsersMarkup();
      } else if (state.adminTab === "import") {
        content.innerHTML = adminImportMarkup();
      } else if (state.adminTab === "audit") {
        const data = await api("/api/admin/audit"); state.audit = data.events; content.innerHTML = adminAuditMarkup();
      }
    } catch (error) { content.innerHTML = `<div class="notice error-notice">${esc(error.message)}</div>`; }
  }

  async function loadAdminQuestions() {
    const query = new URLSearchParams({ page: state.adminPage });
    if (state.adminSearch) query.set("q", state.adminSearch);
    if (state.adminSection) query.set("section", state.adminSection);
    const data = await api(`/api/admin/questions?${query}`);
    state.adminQuestions = data.questions;
    $("#adminContent").innerHTML = adminQuestionsMarkup(data);
  }

  function adminQuestionsMarkup(data) {
    const rows = data.questions.map((q) => `<tr class="${q.active ? "" : "disabled-row"}"><td><span class="pill">${LABELS[q.section]}</span></td><td>${q.number}</td><td><div class="admin-question-stem">${esc(q.stem)}</div><small>${q.options.length ? `${q.options.length} 个选项` : "无选项"} · 答案 ${esc(formatAnswer(q.section, q.answer))}</small></td><td><span class="status-label ${q.active ? "on" : "off"}">${q.active ? "启用" : "停用"}</span></td><td><div class="inline-actions"><button class="link-button" data-edit-question="${q.id}">编辑</button>${q.active ? `<button class="link-button danger-link" data-disable-question="${q.id}">停用</button>` : ""}</div></td></tr>`).join("");
    const pageCount = Math.max(1, Math.ceil(data.total / data.page_size));
    return `<section class="panel admin-question-panel"><div class="panel-heading"><div><p class="eyebrow">题库管理</p><h2>题目列表 <small>共 ${data.total} 道</small></h2></div><button class="button primary" data-action="new-question">＋ 新增题目</button></div><form id="questionSearchForm" class="filter-bar"><label>题型<select id="adminQuestionSection"><option value="">全部题型</option>${sectionOptions(state.adminSection)}</select></label><label class="search-field">题干或题号<input id="adminQuestionSearch" value="${esc(state.adminSearch)}" placeholder="输入关键词或题号"></label><button class="button quiet" type="submit">搜索</button></form><div class="table-panel"><table><thead><tr><th>题型</th><th>题号</th><th>题目内容 / 答案</th><th>状态</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="5"><div class="empty-state compact-empty">没有匹配的题目。</div></td></tr>`}</tbody></table></div><div class="pagination"><span>第 ${data.page} / ${pageCount} 页</span><div><button class="button quiet small-button" data-admin-page="${Math.max(1, data.page - 1)}" ${data.page <= 1 ? "disabled" : ""}>上一页</button><button class="button quiet small-button" data-admin-page="${Math.min(pageCount, data.page + 1)}" ${data.page >= pageCount ? "disabled" : ""}>下一页</button></div></div></section>`;
  }

  function adminUsersMarkup() {
    const rows = state.users.map((user) => `<tr><td><strong>${esc(user.display_name)}</strong><small>${esc(user.username)}</small></td><td><span class="status-label ${user.enabled ? "on" : "off"}">${user.enabled ? "启用" : "停用"}</span></td><td>${fmtDate(user.created_at)}</td><td><div class="inline-actions"><button class="link-button" data-reset-user="${user.id}">重置密码</button><button class="link-button" data-toggle-user="${user.id}" data-enabled="${user.enabled ? "1" : "0"}">${user.enabled ? "停用" : "启用"}</button></div></td></tr>`).join("");
    return `<section class="admin-two-col users-admin"><form id="createUserForm" class="panel stack-form"><div class="panel-heading"><div><p class="eyebrow">单个创建</p><h2>创建学生账号</h2></div></div><label>用户名<input name="username" pattern="[A-Za-z0-9_-]{3,40}" minlength="3" maxlength="40" required></label><label>显示姓名<input name="display_name" maxlength="80" required></label><label>初始密码<input name="password" type="password" minlength="8" required></label><p class="fine-print">用户名使用 3–40 位字母、数字、下划线或短横线，密码至少 8 位。</p><div id="createUserError" class="form-error"></div><button class="button primary" type="submit">创建账号</button></form><form id="bulkUsersForm" class="panel stack-form"><div class="panel-heading"><div><p class="eyebrow">批量导入</p><h2>从 CSV 导入</h2></div></div><p class="muted">CSV 表头：username,display_name,password。每行创建一个学生账号。</p><label class="file-drop"><input id="userCsvFile" type="file" accept=".csv,text/csv" required><span>选择 CSV 文件</span><small id="userCsvName">未选择文件</small></label><div id="bulkUsersResult" class="form-error"></div><button class="button quiet" type="submit">导入账号</button></form></section><section class="panel table-panel users-table"><div class="panel-heading"><div><p class="eyebrow">账号列表</p><h2>学生账号 <small>${state.users.length} 个</small></h2></div></div><table><thead><tr><th>账号</th><th>状态</th><th>创建时间</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="4">暂无学生账号。</td></tr>`}</tbody></table></section>`;
  }

  function adminImportMarkup() {
    const preview = state.importPreview;
    return `<section class="admin-two-col import-layout"><div class="panel import-card"><div class="panel-heading"><div><p class="eyebrow">批量整理</p><h2>导入 PDF 题库</h2></div><span class="pill">按原顺序</span></div><p class="muted">上传 PDF 后系统会校验四类题型、题号连续性、答案标记和题量。预览并确认后，按题型和题号覆盖同题。</p><label class="file-drop"><input id="pdfFile" type="file" accept="application/pdf"><span>选择题库 PDF</span><small id="pdfFileName">尚未选择文件</small></label><div class="notice">当前项目 PDF：单项选择 433、不定项选择 500、判断 504、填空 274，共 1,711 题。题库按 PDF 中题型顺序及各题型题号顺序呈现。</div><div id="pdfImportError" class="form-error"></div><button class="button primary" data-action="preview-pdf">解析并预览</button></div><aside class="panel import-note"><div class="warning-symbol">✓</div><h3>导入前核对</h3><p>PDF 解析会提取题干、选项和答案。正式导入前应抽样核对识别结果。标准答案来自 PDF，题库暂不包含逐题解析。</p><p>新上传 PDF 的题量需要符合当前题库目录。预览只暂存本次待导入数据，点击确认后才会更新题库。</p></aside></section>${preview ? `<section class="panel import-preview"><div class="panel-heading"><div><p class="eyebrow">解析结果</p><h2>${esc(preview.source_name)}</h2></div><span class="pill">SHA-256 ${esc(preview.sha256.slice(0, 12))}…</span></div><div class="import-summary">${SECTIONS.map((key) => `<div><small>${LABELS[key]}</small><strong>${preview.counts[key]} 题</strong></div>`).join("")}<div><small>总题量</small><strong>${preview.total} 题</strong></div></div><div class="notice">${preview.new_questions} 道新题 · ${preview.existing_questions_to_update} 道同题号内容将更新。${esc(preview.note)}</div><h3>前 12 题抽样</h3><div class="sample-list">${preview.sample.map((q) => `<article><div><span class="pill">${LABELS[q.section]}</span><strong>第 ${q.number} 题 · PDF 第 ${q.page} 页</strong></div><p>${esc(q.stem)}</p><small>答案：${esc(q.answer)}</small></article>`).join("")}</div><div class="dialog-actions"><button class="button quiet" data-action="clear-preview">取消</button><button class="button primary" data-action="commit-pdf">确认导入 ${preview.total} 题</button></div></section>` : ""}`;
  }

  function adminAuditMarkup() {
    const rows = state.audit.map((event) => `<tr><td>${fmtDate(event.created_at)}</td><td>${esc(event.username)}</td><td><strong>${esc(event.action)}</strong></td><td>${esc(event.details)}</td></tr>`).join("");
    return `<section class="panel table-panel"><div class="panel-heading"><div><p class="eyebrow">管理留痕</p><h2>最近操作</h2></div><span class="pill">最近 100 条</span></div><table><thead><tr><th>时间</th><th>管理员</th><th>操作</th><th>详情</th></tr></thead><tbody>${rows || `<tr><td colspan="4">暂无管理员操作记录。</td></tr>`}</tbody></table></section>`;
  }

  function openQuestionDialog(question = null) {
    const form = $("#questionForm");
    $("#questionDialogTitle").textContent = question ? "编辑题目" : "新增题目";
    $("#questionId").value = question?.id || "";
    $("#questionSection").innerHTML = sectionOptions(question?.section || "single");
    $("#questionNumber").value = question?.number || "";
    $("#questionPage").value = question?.source_page || 0;
    $("#questionSubject").value = question?.subject || "";
    $("#questionStem").value = question?.stem || "";
    $("#questionOptions").value = (question?.options || []).map((option) => `${option.key} | ${option.text}`).join("\n");
    $("#questionAnswer").value = question?.answer || "";
    $("#questionExplanation").value = question?.explanation || "";
    $("#questionFormError").textContent = "";
    $("#editDialog").showModal();
    if (!question) $("#questionNumber").focus();
  }

  async function saveQuestion(event) {
    event.preventDefault();
    const optionsText = $("#questionOptions").value.trim();
    const options = optionsText ? optionsText.split(/\r?\n/).map((line) => {
      const match = line.match(/^\s*([A-F])\s*[|.、:]\s*(.+)$/i);
      if (!match) throw new Error("选项格式需为“A | 选项文字”，每行一项。");
      return { key: match[1].toUpperCase(), text: match[2].trim() };
    }) : [];
    const body = { id: $("#questionId").value || null, section: $("#questionSection").value, number: Number($("#questionNumber").value), source_page: Number($("#questionPage").value || 0), subject: $("#questionSubject").value, stem: $("#questionStem").value, options, answer: $("#questionAnswer").value, explanation: $("#questionExplanation").value };
    try {
      await api("/api/admin/questions", { method: "POST", body });
      $("#editDialog").close(); toast("题目已保存。", "success"); state.adminPage = 1; await loadAdminQuestions();
    } catch (error) { $("#questionFormError").textContent = error.message; }
  }

  function parseCsv(text) {
    const rows = []; let row = [], field = "", quoted = false;
    for (let i = 0; i < text.length; i++) {
      const char = text[i];
      if (quoted && char === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (char === '"') quoted = !quoted;
      else if (char === "," && !quoted) { row.push(field); field = ""; }
      else if ((char === "\n" || char === "\r") && !quoted) {
        if (char === "\r" && text[i + 1] === "\n") i++;
        row.push(field); if (row.some((item) => item.trim())) rows.push(row); row = []; field = "";
      } else field += char;
    }
    row.push(field); if (row.some((item) => item.trim())) rows.push(row);
    const headers = (rows.shift() || []).map((value) => value.trim().replace(/^\uFEFF/, "").toLowerCase());
    const indexes = { username: headers.indexOf("username"), display_name: headers.indexOf("display_name"), password: headers.indexOf("password") };
    if (Object.values(indexes).some((index) => index < 0)) throw new Error("CSV 必须包含 username、display_name、password 三列。");
    return rows.map((values) => ({ username: values[indexes.username]?.trim(), display_name: values[indexes.display_name]?.trim(), password: values[indexes.password]?.trim() }));
  }

  async function submitUserCsv(event) {
    event.preventDefault();
    const file = $("#userCsvFile").files[0];
    if (!file) return;
    try {
      const users = parseCsv(await file.text());
      const data = await api("/api/admin/users/import", { method: "POST", body: { users } });
      const resultText = `成功创建 ${data.created.length} 个账号${data.errors.length ? `；${data.errors.length} 行未导入：${data.errors.map((item) => `第${item.row}行 ${item.username || ""} ${item.error}`).join("；")}` : "。"}`;
      const resultClass = data.errors.length ? "form-error" : "form-success";
      state.users = (await api("/api/admin/users")).users;
      $("#adminContent").innerHTML = adminUsersMarkup();
      $("#bulkUsersResult").textContent = resultText;
      $("#bulkUsersResult").className = resultClass;
      toast(`已创建 ${data.created.length} 个学生账号。`, "success");
    } catch (error) { $("#bulkUsersResult").textContent = error.message; }
  }

  async function previewPdf() {
    const file = $("#pdfFile").files[0];
    if (!file) return $("#pdfImportError").textContent = "请先选择 PDF 文件。";
    try {
      const data = await api("/api/admin/import/pdf", { method: "POST", headers: { "Content-Type": "application/pdf", "X-File-Name": encodeURIComponent(file.name) }, body: file });
      state.importPreview = data; await loadAdminTab();
    } catch (error) { $("#pdfImportError").textContent = error.message; }
  }

  async function commitPdf() {
    if (!state.importPreview) return;
    if (!window.confirm(`确认导入 ${state.importPreview.total} 道题？同题型、同题号的题目内容会更新。`)) return;
    try {
      const result = await api("/api/admin/import/commit", { method: "POST", body: { job_id: state.importPreview.job_id } });
      state.importPreview = null; toast(`已导入 ${result.imported} 道题。`, "success"); await loadAdminTab();
    } catch (error) { toast(error.message, "error"); }
  }

  function showError(error) {
    $("#mainContent").innerHTML = `<section class="panel error-panel"><span class="error-mark">!</span><h2>暂时无法加载</h2><p>${esc(error.message)}</p><button class="button quiet" data-action="retry-view">重试</button></section>`;
  }

  document.addEventListener("click", async (event) => {
    const viewButton = event.target.closest("[data-view]");
    if (viewButton) { event.preventDefault(); setView(viewButton.dataset.view); return; }
    const practiceButton = event.target.closest("[data-practice-section]");
    if (practiceButton) { startPractice(practiceButton.dataset.practiceSection); return; }
    const adminTab = event.target.closest("[data-admin-tab]");
    if (adminTab) { state.adminTab = adminTab.dataset.adminTab; await renderAdmin(); return; }
    const examIndex = event.target.closest("[data-exam-index]");
    if (examIndex) { queueExamAnswer(); state.examIndex = Number(examIndex.dataset.examIndex); renderExam(); return; }
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action === "submit-practice") submitPractice();
    else if (action === "practice-next") loadPracticeQuestion();
    else if (action === "practice-home") { state.practice.section = null; state.practice.question = null; renderPracticeHome(); }
    else if (action === "favorite") toggleFavorite(event.target.closest("[data-question]").dataset.question);
    else if (action === "start-exam") startExam();
    else if (action === "exam-prev") { queueExamAnswer(); state.examIndex = Math.max(0, state.examIndex - 1); renderExam(); }
    else if (action === "exam-next") { queueExamAnswer(); state.examIndex = Math.min(state.exam.items.length - 1, state.examIndex + 1); renderExam(); }
    else if (action === "submit-exam") submitExam(false);
    else if (action === "collection-more") loadCollection(state.collection.kind, state.collection.page + 1, true);
    else if (action === "new-question") openQuestionDialog();
    else if (action === "preview-pdf") previewPdf();
    else if (action === "commit-pdf") commitPdf();
    else if (action === "clear-preview") { state.importPreview = null; await loadAdminTab(); }
    else if (action === "retry-view") setView(state.view);
    const openQuestion = event.target.closest("[data-open-question]");
    if (openQuestion) { state.practice.section = openQuestion.dataset.openQuestion; await loadPracticeQuestion(openQuestion.dataset.number); return; }
    const edit = event.target.closest("[data-edit-question]");
    if (edit) openQuestionDialog(state.adminQuestions.find((q) => q.id === Number(edit.dataset.editQuestion)));
    const disable = event.target.closest("[data-disable-question]");
    if (disable && window.confirm("确定停用这道题吗？停用后新练习和考试不会再使用它。")) {
      try { await api(`/api/admin/questions/${disable.dataset.disableQuestion}`, { method: "DELETE" }); toast("题目已停用。", "success"); await loadAdminQuestions(); }
      catch (error) { toast(error.message, "error"); }
    }
    const page = event.target.closest("[data-admin-page]");
    if (page) { state.adminPage = Number(page.dataset.adminPage); await loadAdminQuestions(); }
    const review = event.target.closest("[data-review]");
    if (review) openReview(review.dataset.review);
    const reset = event.target.closest("[data-reset-user]");
    if (reset) {
      const password = window.prompt("输入新的初始密码（至少 8 位）：");
      if (password) try { await api(`/api/admin/users/${reset.dataset.resetUser}/reset-password`, { method: "POST", body: { password } }); toast("密码已重置。", "success"); } catch (error) { toast(error.message, "error"); }
    }
    const toggle = event.target.closest("[data-toggle-user]");
    if (toggle) {
      const enabled = toggle.dataset.enabled !== "1";
      if (window.confirm(enabled ? "确定启用这个学生账号吗？" : "确定停用这个学生账号吗？")) {
        try { await api(`/api/admin/users/${toggle.dataset.toggleUser}/toggle`, { method: "POST", body: { enabled } }); state.users = (await api("/api/admin/users")).users; await loadAdminTab(); toast("账号状态已更新。", "success"); } catch (error) { toast(error.message, "error"); }
      }
    }
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches("input[name=answerChoice]")) {
      const q = state.view === "exam" ? state.exam?.items[state.examIndex] : state.practice.question;
      if (state.view === "exam" && state.exam) { queueExamAnswer(); renderExam(); }
      else if (q) { state.practice.answer = readPracticeAnswer(); renderPracticeView(); }
    }
    if (event.target.id === "examDuration" || event.target.matches("[data-count]")) {
      const total = SECTIONS.reduce((sum, key) => sum + Number($(`[data-count="${key}"]`)?.value || 0), 0);
      if ($("#examTotal")) $("#examTotal").textContent = total;
    }
    if (event.target.id === "userCsvFile") $("#userCsvName").textContent = event.target.files[0]?.name || "未选择文件";
    if (event.target.id === "pdfFile") $("#pdfFileName").textContent = event.target.files[0]?.name || "尚未选择文件";
  });

  document.addEventListener("input", (event) => {
    if (event.target.id === "fillAnswer") {
      if (state.view === "exam") queueExamAnswer();
      else {
        state.practice.answer = event.target.value;
        const button = $("[data-action=submit-practice]");
        if (button) button.disabled = !event.target.value.trim();
      }
    }
    if (event.target.matches("[data-count]")) {
      const total = SECTIONS.reduce((sum, key) => sum + Number($(`[data-count="${key}"]`)?.value || 0), 0);
      if ($("#examTotal")) $("#examTotal").textContent = total;
    }
  });

  document.addEventListener("submit", async (event) => {
    if (event.target.id === "loginForm") {
      event.preventDefault(); $("#loginError").textContent = "";
      try {
        const data = await api("/api/login", { method: "POST", body: { username: $("#loginUsername").value.trim(), password: $("#loginPassword").value } });
        state.user = data.user; state.csrf = data.user.csrf_token; $("#loginPassword").value = ""; showApp(); setView("dashboard");
      } catch (error) { $("#loginError").textContent = error.message; }
    } else if (event.target.id === "questionForm") {
      try { await saveQuestion(event); } catch (error) { $("#questionFormError").textContent = error.message; }
    } else if (event.target.id === "settingsForm") saveSettings(event);
    else if (event.target.id === "questionSearchForm") {
      event.preventDefault(); state.adminSection = $("#adminQuestionSection").value; state.adminSearch = $("#adminQuestionSearch").value.trim(); state.adminPage = 1; loadAdminQuestions();
    } else if (event.target.id === "createUserForm") {
      event.preventDefault(); const form = new FormData(event.target);
      try { await api("/api/admin/users", { method: "POST", body: Object.fromEntries(form) }); state.users = (await api("/api/admin/users")).users; await loadAdminTab(); toast("学生账号已创建。", "success"); }
      catch (error) { $("#createUserError").textContent = error.message; }
    } else if (event.target.id === "bulkUsersForm") submitUserCsv(event);
    else if (event.target.id === "passwordForm") {
      event.preventDefault();
      try { await api("/api/password", { method: "POST", body: { old_password: $("#oldPassword").value, new_password: $("#newPassword").value } }); $("#passwordDialog").close(); event.target.reset(); toast("密码已更新。", "success"); }
      catch (error) { $("#passwordFormError").textContent = error.message; }
    }
  });

  $("#logoutButton").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST", body: {} }); } catch (_) { /* local logout proceeds */ }
    state.user = null; state.csrf = ""; state.exam = null; stopTimer(); showLogin();
  });
  $("#passwordButton").addEventListener("click", () => { $("#passwordFormError").textContent = ""; $("#passwordDialog").showModal(); });
  $("#questionForm").addEventListener("submit", (event) => {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.exam && state.view === "exam") { event.preventDefault(); event.returnValue = ""; }
  });

  async function loadExamView() {
    stopTimer();
    try {
      const current = await api("/api/exams/current");
      if (current.result) { renderExamResult(current.result, "考试时间结束，系统已自动交卷。"); return; }
      if (current.exam) { state.exam = current.exam; state.examIndex = 0; renderExam(); return; }
      const [configData, bank] = await Promise.all([api("/api/exam/config"), api("/api/bank")]);
      const config = configData.config;
      const total = SECTIONS.reduce((sum, key) => sum + config.counts[key], 0);
      const maxScore = SECTIONS.reduce((sum, key) => sum + config.counts[key] * config.points[key], 0);
      const bankTotal = Object.values(bank.counts).reduce((sum, amount) => sum + amount, 0);
      const rulesNote = config.official_verified
        ? `管理员已按正式规则核验：${esc(config.official_reference)}`
        : "当前为示例参数，尚未核验到可引用的官方考试细则。请以正式规则为准。";
      $("#mainContent").innerHTML = `<section class="page-heading"><div><p class="eyebrow">模拟考试</p><h1>开始一场限时自测</h1><p class="muted">系统按题型分别随机抽题，每场考试题目固定，切换页面或重新登录可继续作答。</p></div></section><section class="exam-start-layout"><div class="panel exam-config-card"><div class="exam-icon">◷</div><p class="eyebrow">当前考试模板</p><h2>${esc(config.name)}</h2><p class="muted">参数由管理员设置。${rulesNote}</p><div class="exam-facts"><div><small>考试时长</small><strong>${config.duration_minutes} 分钟</strong></div><div><small>题目总数</small><strong>${total} 题</strong></div><div><small>满分</small><strong>${maxScore} 分</strong></div></div><div class="exam-type-list">${SECTIONS.map((key) => `<div><span>${LABELS[key]}</span><span>${config.counts[key]} 题 · 每题 ${config.points[key]} 分${(config.wrong_penalties?.[key] ?? 0) > 0 ? ` · 答错扣 ${config.wrong_penalties[key]} 分` : ""}</span></div>`).join("")}</div><button class="button primary wide-button" data-action="start-exam">开始模拟考试 <span>→</span></button><p class="fine-print">开始后计时立即启动；答案会在交卷后显示。</p></div><aside class="panel exam-rules"><h3>考试说明</h3><div class="rule-item"><span>01</span><p>题目从当前启用题库中随机抽取，题型数量按模板固定。</p></div><div class="rule-item"><span>02</span><p>作答过程自动保存；倒计时结束时系统会自动交卷。</p></div><div class="rule-item"><span>03</span><p>可提前交卷，提交后查看总分、逐题正误、正确答案和已有解析。</p></div><div class="rule-item"><span>04</span><p>满分 ${maxScore} 分 · 题库当前共 ${bankTotal} 题。</p></div></aside></section>`;
    } catch (error) { showError(error); }
  }

  function adminSettingsMarkup(config, official) {
    const total = SECTIONS.reduce((sum, key) => sum + config.counts[key], 0);
    const maximum = SECTIONS.reduce((sum, key) => sum + config.counts[key] * config.points[key], 0);
    return `<section class="admin-two-col"><form id="settingsForm" class="panel settings-form"><div class="panel-heading"><div><p class="eyebrow">组卷方案</p><h2>模拟考试参数</h2></div><span class="pill">可随时调整</span></div><label>模板名称<input id="examName" maxlength="80" value="${esc(config.name)}" required></label><label>考试时长（分钟）<input id="examDuration" type="number" min="1" max="600" value="${config.duration_minutes}" required></label><div class="field-heading"><strong>题型数量、分值与扣分</strong><small>总题量：<b id="examTotal">${total}</b></small></div><div class="settings-grid"><div class="settings-head"><span>题型</span><span>题量</span><span>每题分值</span><span>答错扣分</span></div>${SECTIONS.map((key) => `<div class="settings-row"><label>${LABELS[key]}</label><input aria-label="${LABELS[key]}题量" type="number" min="0" max="1000" data-count="${key}" value="${config.counts[key]}"><input aria-label="${LABELS[key]}每题分值" type="number" min="0" max="100" step="0.25" data-points="${key}" value="${config.points[key]}"><input aria-label="${LABELS[key]}答错扣分" type="number" min="0" max="100" step="0.25" data-penalty="${key}" value="${config.wrong_penalties?.[key] ?? 0}"></div>`).join("")}</div><p class="fine-print">扣分仅用于未获得部分分的错误作答；未作答不扣分。开启部分得分时，已获得的部分分不再扣分。</p><label class="check-field"><input id="multiplePartial" type="checkbox" ${config.multiple_partial_credit ? "checked" : ""}><span>不定项选择题：未选错项时按已选正确项比例给分</span></label><label class="check-field"><input id="fillPartial" type="checkbox" ${config.fill_partial_credit ? "checked" : ""}><span>填空题按空给部分分</span></label><label>正式规则来源（文件名、网址或通知名称）<input id="officialReference" maxlength="500" value="${esc(config.official_reference || "")}" placeholder="尚未核验时留空"></label><label class="check-field"><input id="officialVerified" type="checkbox" ${official ? "checked" : ""}><span>我已根据上述正式规则核验当前参数和评分方式</span></label><div id="settingsError" class="form-error"></div><button class="button primary" type="submit">保存考试参数</button></form><aside class="panel config-note"><div class="warning-symbol">!</div><h3>${official ? "已配置正式参数" : "当前参数为示例值"}</h3><p>${official ? `规则来源：${esc(config.official_reference)}` : "暂未找到可核验的官方考试时长、题量比例和评分细则。系统预设为 120 分钟、100 题（25 / 25 / 30 / 20），每题 1 分。请以正式文件为准修改。"}</p><div class="config-note-facts"><span>当前题量</span><strong>${total} 题</strong><span>满分</span><strong>${maximum} 分</strong></div><p class="fine-print">修改仅影响新开始的考试，进行中的考试仍使用开始时保存的设置。</p></aside></section>`;
  }

  async function saveSettings(event) {
    event.preventDefault();
    const config = {
      name: $("#examName").value,
      duration_minutes: Number($("#examDuration").value),
      counts: {}, points: {}, wrong_penalties: {},
      multiple_partial_credit: $("#multiplePartial").checked,
      fill_partial_credit: $("#fillPartial").checked,
      official_reference: $("#officialReference").value.trim(),
      official_verified: $("#officialVerified").checked,
    };
    SECTIONS.forEach((key) => {
      config.counts[key] = Number($(`[data-count="${key}"]`).value);
      config.points[key] = Number($(`[data-points="${key}"]`).value);
      config.wrong_penalties[key] = Number($(`[data-penalty="${key}"]`).value);
    });
    try {
      await api("/api/admin/settings", { method: "POST", body: { config } });
      toast("考试参数已保存。", "success");
      await loadAdminTab();
    } catch (error) { $("#settingsError").textContent = error.message; }
  }

  boot();
})();
