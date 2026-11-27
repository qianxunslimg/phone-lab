(() => {
  const host = window.location.hostname || "127.0.0.1";
  const $ = (id) => document.getElementById(id);

  $("link-fb").href = `http://${host}:8080`;
  $("link-tty").href = `http://${host}:7681`;

  let sortBy = "cpu";
  let accLoaded = false;
  let smsLoaded = false;
  let activeTab = "overview";
  let authToken = sessionStorage.getItem("phone_srv_auth") || "";

  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 2600);
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function copyText(txt, label) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(txt).then(() => toast(`已复制${label || ""}: ${txt}`));
    } else {
      const ta = document.createElement("textarea");
      ta.value = txt;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      toast(`已复制${label || ""}: ${txt}`);
    }
  }

  function updateControlAuthUI() {
    const authed = Boolean(authToken);
    const needAuth = activeTab === "control" || activeTab === "sms";
    $("tab-overview").classList.toggle("hidden", activeTab !== "overview");
    $("tab-control").classList.toggle("hidden", !needAuth || (authed && activeTab !== "control"));
    $("auth-gate").classList.toggle("hidden", authed || !needAuth);
    $("control-panel").classList.toggle("hidden", !authed || activeTab !== "control");
    $("tab-sms").classList.toggle("hidden", !authed || activeTab !== "sms");
    if (authed && activeTab === "control") {
      loadAccConfig();
    }
    if (authed && activeTab === "sms") {
      loadSmsStatus();
    }
  }

  function lockControlCenter(msg) {
    authToken = "";
    accLoaded = false;
    smsLoaded = false;
    sessionStorage.removeItem("phone_srv_auth");
    $("auth-pass").value = "";
    updateControlAuthUI();
    if (msg) toast(msg);
  }

  function fmtBytes(b) {
    if (!b) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let val = b;
    while (val >= 1024 && i < units.length - 1) {
      val /= 1024;
      i += 1;
    }
    return `${val.toFixed(1)} ${units[i]}`;
  }

  function fmtSpeed(kbps) {
    return kbps >= 1024 ? `${(kbps / 1024).toFixed(2)} MB/s` : `${kbps.toFixed(1)} KB/s`;
  }

  function barCls(pct) {
    return pct > 85 ? "bad" : pct > 65 ? "warn" : "ok";
  }

  function drawSpark(svgId, seriesList, maxFixed) {
    const svg = $(svgId);
    if (!svg) return;
    const W = 240;
    const H = 42;
    const pad = 3;
    let maxVal = maxFixed || 1;
    if (!maxFixed) {
      seriesList.forEach((s) => s.data.forEach((v) => {
        if (v > maxVal) maxVal = v;
      }));
      maxVal *= 1.15;
    }
    svg.innerHTML = seriesList.map((s) => {
      const arr = s.data.length > 1 ? s.data : [0, s.data[0] || 0];
      const pts = arr.map((v, idx) => {
        const x = (idx / Math.max(arr.length - 1, 1)) * W;
        const y = H - pad - (Math.min(v, maxVal) / maxVal) * (H - pad * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" ");
      return `<polygon points="0,${H} ${pts} ${W},${H}" fill="${s.fill}" />
              <polyline points="${pts}" fill="none" stroke="${s.stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />`;
    }).join("");
  }

  async function apiPost(url, body = {}) {
    if (!authToken) {
      lockControlCenter("请先登录控制中心");
      return null;
    }
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: authToken,
      },
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      lockControlCenter("认证已失效，请重新登录");
      return null;
    }
    const data = await res.json();
    toast(data.message || (data.ok ? "操作成功" : "操作失败"));
    return data;
  }

  function syncSliderLabels() {
    $("val-rc").textContent = `${$("inp-rc").value}%`;
    $("val-pc").textContent = `${$("inp-pc").value}%`;
    $("val-mt").textContent = `${$("inp-mt").value}°C`;
    $("val-rt").textContent = `${$("inp-rt").value}°C`;
  }

  async function loadAccConfig() {
    if (!authToken) return;
    try {
      const res = await fetch("/api/acc", { headers: { Authorization: authToken } });
      if (res.status === 401) {
        lockControlCenter("认证已失效，请重新登录");
        return;
      }
      const c = await res.json();
      if (!c.available) {
        $("acc-status-tag").textContent = "未挂载";
        $("acc-status-tag").className = "tag warn";
        return;
      }
      $("acc-status-tag").textContent = `${c.resume_capacity}% ~ ${c.pause_capacity}%`;
      $("acc-status-tag").className = "tag ok";
      $("ctrl-mtk-tag").textContent = `PowerPath:${c.power_path ? "ON" : "OFF"} · cmd:${c.current_cmd}`;
      if (!accLoaded) {
        $("inp-rc").value = c.resume_capacity;
        $("inp-pc").value = c.pause_capacity;
        $("inp-mt").value = c.max_temp;
        $("inp-rt").value = c.resume_temp;
        $("inp-idle").checked = !!c.prioritize_idle;
        syncSliderLabels();
        accLoaded = true;
      }
    } catch {
      $("acc-status-tag").textContent = "读取失败";
    }
  }

  async function loadInlineLog(name) {
    if (!authToken) {
      lockControlCenter("请先登录控制中心");
      return;
    }
    $("inline-log-content").textContent = `正在加载 ${name} 日志...`;
    const res = await fetch(`/api/logs?name=${encodeURIComponent(name)}`, {
      headers: { Authorization: authToken },
    });
    if (res.status === 401) {
      lockControlCenter("认证已失效，请重新登录");
      return;
    }
    const data = await res.json();
    $("inline-log-content").textContent = data.content || "(暂无日志)";
  }

  function renderSmsStatus(data, forceFillForm = false) {
    if (!data || !data.config) return;
    const c = data.config;
    const tag = $("sms-state-tag");
    if (!data.db_available) {
      tag.textContent = "短信库不可访问";
      tag.className = "tag warn";
    } else if (c.enabled) {
      tag.textContent = "● 监听转发中";
      tag.className = "tag ok";
    } else {
      tag.textContent = "未开启";
      tag.className = "tag warn";
    }

    $("sms-pass-hint").textContent = c.smtp_pass_set ? "（已保存）" : "（未设置）";
    $("sms-meta-line").textContent =
      `累计已转发：${c.forwarded_count || 0} 条` +
      (c.last_sent_at ? ` · 最近发送：${c.last_sent_at}` : "") +
      ` · 当前游标 ID #${c.last_sms_id || 0}`;

    const errBox = $("sms-err-box");
    if (c.last_error) {
      errBox.textContent = `最近错误：${c.last_error}`;
      errBox.classList.remove("hidden");
    } else {
      errBox.classList.add("hidden");
    }

    if (!smsLoaded || forceFillForm) {
      $("sms-enabled").checked = Boolean(c.enabled);
      $("sms-only-otp").checked = Boolean(c.only_otp);
      $("sms-smtp-user").value = c.smtp_user || "";
      $("sms-to-email").value = c.to_email || "";
      $("sms-smtp-host").value = c.smtp_host || "smtp.qq.com";
      $("sms-smtp-port").value = c.smtp_port || 465;

      const selected = new Set((c.sim_ids || []).map(Number));
      const sims = data.sims || [];
      if (sims.length === 0) {
        $("sms-sim-list").innerHTML = `<div class="sub">未检测到 SIM 卡记录（默认监听全部短信）</div>`;
      } else {
        $("sms-sim-list").innerHTML = sims.map((s) => `
          <label class="switch-row">
            <input type="checkbox" class="sms-sim-chk" value="${s.sub_id}" ${selected.has(s.sub_id) ? "checked" : ""} />
            <span><b>${escapeHtml(s.slot_label)}</b> · ${escapeHtml(s.display_name)}${s.number ? ` <code>${escapeHtml(s.number)}</code>` : ""}</span>
          </label>
        `).join("");
      }
      smsLoaded = true;
    }

    const inbox = data.recent_sms || [];
    $("sms-inbox-cnt").textContent = `${inbox.length} 条`;
    if (inbox.length === 0) {
      $("sms-inbox-list").innerHTML = `<div class="sub">暂无收件箱短信（收到新短信后会自动出现在这里并转发）</div>`;
    } else {
      $("sms-inbox-list").innerHTML = inbox.map((m) => `
        <div class="sms-item">
          <div class="sms-item-hd">
            <span><b>${escapeHtml(m.address)}</b> · <span class="tag">${escapeHtml(m.sim_label)}</span></span>
            ${m.otp ? `<span class="otp-badge" data-copy-otp="${escapeHtml(m.otp)}" title="点击复制验证码">验证码 ${escapeHtml(m.otp)} 📋</span>` : ""}
          </div>
          <div class="sms-body">${escapeHtml(m.body)}</div>
          <div class="sms-foot">
            <span>${escapeHtml(m.time)} · #${m.id}</span>
            <button class="btn sm secondary" data-resend-sms="${m.id}" type="button">✉️ 转发到邮箱</button>
          </div>
        </div>
      `).join("");
    }

    const hist = data.history || [];
    $("sms-hist-cnt").textContent = `${hist.length} 条`;
    if (hist.length === 0) {
      $("sms-hist-body").innerHTML = `<tr><td colspan="5" class="sub">暂无转发记录</td></tr>`;
    } else {
      $("sms-hist-body").innerHTML = hist.map((h) => {
        const stCls = h.status === "ok" ? "ok" : "warn";
        const stTxt = h.status === "ok" ? "已发送" : h.status === "skipped" ? "已过滤" : "失败";
        return `
          <tr>
            <td>${escapeHtml(h.forwarded_at || h.time)}</td>
            <td>${escapeHtml(h.sim_label)}</td>
            <td><code>${escapeHtml(h.address)}</code></td>
            <td><span class="tag ${stCls}">${stTxt}</span></td>
            <td>${h.otp ? `<b>[${escapeHtml(h.otp)}]</b> ` : ""}${escapeHtml(h.detail || h.body)}</td>
          </tr>
        `;
      }).join("");
    }
  }

  async function loadSmsStatus(forceFillForm = false) {
    if (!authToken) return;
    try {
      const res = await fetch("/api/sms/status", { headers: { Authorization: authToken } });
      if (res.status === 401) {
        lockControlCenter("认证已失效，请重新登录");
        return;
      }
      const data = await res.json();
      renderSmsStatus(data, forceFillForm);
    } catch {
      $("sms-state-tag").textContent = "读取失败";
    }
  }

  function collectSmsForm() {
    const simIds = Array.from(document.querySelectorAll(".sms-sim-chk:checked")).map((el) => Number(el.value));
    const payload = {
      enabled: $("sms-enabled").checked,
      only_otp: $("sms-only-otp").checked,
      smtp_user: $("sms-smtp-user").value.trim(),
      to_email: $("sms-to-email").value.trim(),
      smtp_host: $("sms-smtp-host").value.trim() || "smtp.qq.com",
      smtp_port: Number($("sms-smtp-port").value) || 465,
      smtp_ssl: true,
      sim_ids: simIds,
    };
    const pass = $("sms-smtp-pass").value.trim();
    if (pass) payload.smtp_pass = pass;
    return payload;
  }

  function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    updateControlAuthUI();
  }

  function setSort(mode) {
    sortBy = mode;
    $("btn-sort-cpu").classList.toggle("active", mode === "cpu");
    $("btn-sort-mem").classList.toggle("active", mode === "mem");
    refresh();
  }

  async function refresh() {
    try {
      const res = await fetch(`/api/metrics?sort=${sortBy}`);
      const d = await res.json();
      const ip = d.network.ip || host;

      $("live-dot").textContent = "● 在线";
      $("meta").textContent = `Ubuntu 24.04 ARM64 (${d.hostname}) · 已运行 ${d.uptime} · ${d.timestamp}`;
      const savedUser = localStorage.getItem("phone_srv_user") || "ubuntu";
      $("ssh-cmd").textContent = `ssh ${savedUser}@${ip}`;

      // Battery
      const b = d.battery;
      const th = d.thermals || {};
      $("bat-cap").textContent = `${b.capacity}%`;
      $("bat-temp").innerHTML = `电池 <b>${b.temp_c}°C</b><br>充电IC ${th.charger ?? "--"}°C`;
      $("bat-bar").style.width = `${b.capacity}%`;
      $("bat-bar").className = b.capacity < 20 ? "bad" : "ok";
      $("bypass-tag").textContent = b.bypass_active ? "⚡ 直驱/Idle" : (b.power_path ? "PowerPath 就绪" : b.status);
      $("bypass-tag").className = `tag ${b.bypass_active ? "ok" : "warn"}`;
      $("bat-sub").innerHTML =
        `状态: <b>${b.status}</b> · 功率: <b>${b.power_w}W</b> (${b.volt_v}V / ${b.curr_ma}mA)<br>` +
        `健康度: <b>${b.health_pct}%</b> · 循环: ${b.cycles} 次`;

      // CPU
      $("cpu-pct").textContent = `${d.cpu_pct}%`;
      $("cpu-load").textContent = `AP ${th.ap ?? "--"}°C · Load ${d.load.split(" / ")[0]}`;
      $("cpu-temp-tag").textContent = `CPU ${th.cpu ?? "--"}°C`;
      drawSpark("spark-cpu", [{ data: d.history.cpu, stroke: "#4f8cff", fill: "rgba(79,140,255,.16)" }], 100);
      $("cpu-cores").innerHTML = d.cpu_cores.map((c) => `
        <div class="core" title="CPU${c.id}: ${c.cur_mhz}/${c.max_mhz} MHz">
          <div class="core-bar"><div class="core-fill ${c.id >= 6 ? "bigcore" : ""}" style="height:${c.pct}%"></div></div>
          <div class="core-lbl">${(c.cur_mhz / 1000).toFixed(1)}G</div>
        </div>
      `).join("");

      // Memory
      const m = d.memory;
      $("mem-pct").textContent = `${m.pct}%`;
      $("mem-avail-tag").textContent = `可用 ${fmtBytes(m.avail)}`;
      $("mem-used-top").textContent = `${fmtBytes(m.used)} / ${fmtBytes(m.total)}`;
      drawSpark("spark-mem", [{ data: d.history.mem, stroke: "#34d399", fill: "rgba(52,211,153,.15)" }], 100);
      $("mem-sub").innerHTML = `缓存/Buff: <b>${fmtBytes(m.cached)}</b> · Swap: ${fmtBytes(m.swap_used)} / ${fmtBytes(m.swap_total)}`;

      // Network
      const n = d.network;
      $("net-ip-tag").textContent = n.ip;
      $("net-speed").innerHTML = `<span style="color:var(--green)">↓ ${fmtSpeed(n.rx_kbps)}</span> <span style="color:var(--muted)">/</span> <span style="color:var(--cyan)">↑ ${fmtSpeed(n.tx_kbps)}</span>`;
      drawSpark("spark-net", [
        { data: d.history.rx_kbps, stroke: "#34d399", fill: "rgba(52,211,153,.14)" },
        { data: d.history.tx_kbps, stroke: "#22d3ee", fill: "rgba(34,211,238,.10)" },
      ], 0);
      $("net-sub").innerHTML = `累计下载: <b>${fmtBytes(n.rx_bytes)}</b> · 累计上传: <b>${fmtBytes(n.tx_bytes)}</b>`;

      // Disks
      $("disks").innerHTML = d.disks.map((dk) => `
        <div class="disk-item">
          <div class="disk-hd">
            <span><b>${dk.label}</b> <code>${dk.path}</code></span>
            <span>${dk.pct}%</span>
          </div>
          <div class="bar"><div class="${barCls(dk.pct)}" style="width:${dk.pct}%"></div></div>
          <div>已用 <b>${fmtBytes(dk.used)}</b> / 共 ${fmtBytes(dk.total)}（剩余 <b>${fmtBytes(dk.free)}</b>）</div>
        </div>
      `).join("");

      // Services: Overview is strictly read-only; controls live in Control Center
      $("services").innerHTML = Object.entries(d.services).map(([k, s]) => `
        <div class="svc">
          <div class="svc-left">
            <span class="dot ${s.running ? "on" : ""}"></span>
            <div>
              <div><b>${k}</b></div>
              <div class="svc-meta">${s.running ? `PID ${s.pid} · ${s.rss_mb} MB` : "已停止"}</div>
            </div>
          </div>
          <span class="tag ${s.running ? "ok" : "warn"}">${s.running ? "运行中" : "已停止"}</span>
        </div>
      `).join("");

      $("ctrl-services").innerHTML = ["filebrowser", "ttyd"].map((k) => {
        const s = d.services[k] || { running: false, pid: "-", rss_mb: 0 };
        return `
          <div class="svc">
            <div class="svc-left">
              <span class="dot ${s.running ? "on" : ""}"></span>
              <div>
                <div><b>${k}</b></div>
                <div class="svc-meta">${s.running ? `运行中 · ${s.rss_mb} MB` : "已停止"}</div>
              </div>
            </div>
            <div class="ctrl-actions">
              <button class="btn sm secondary" data-svc="${k}" data-act="restart" type="button">重启</button>
              <button class="btn sm secondary" data-svc="${k}" data-act="${s.running ? "stop" : "start"}" type="button">${s.running ? "停止" : "启动"}</button>
            </div>
          </div>
        `;
      }).join("");

      // Ports
      $("port-cnt").textContent = `${d.ports.length} 个`;
      $("ports").innerHTML = d.ports.map((p) => `
        <tr><td><b>:${p.port}</b></td><td><code>${p.addr}</code></td><td><code>${p.proc}</code></td></tr>
      `).join("");

      // Processes
      $("procs").innerHTML = d.top_procs.map((p) => `
        <tr>
          <td>${p.pid}</td>
          <td><code>${p.cmd}</code></td>
          <td class="num">${p.cpu}%</td>
          <td class="num">${p.mem}%</td>
          <td class="num">${p.rss_mb}M</td>
        </tr>
      `).join("");
    } catch {
      $("live-dot").textContent = "● 断开重连中";
    }
  }

  // Event bindings
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  $("btn-copy-ssh").addEventListener("click", () => {
    copyText($("ssh-cmd").textContent);
  });

  $("btn-sort-cpu").addEventListener("click", () => setSort("cpu"));
  $("btn-sort-mem").addEventListener("click", () => setSort("mem"));

  ["inp-rc", "inp-pc", "inp-mt", "inp-rt"].forEach((id) => {
    $(id).addEventListener("input", syncSliderLabels);
  });

  document.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const [rc, pc] = btn.dataset.preset.split(",");
      $("inp-rc").value = rc;
      $("inp-pc").value = pc;
      syncSliderLabels();
    });
  });

  $("btn-save-acc").addEventListener("click", async () => {
    const data = await apiPost("/api/acc", {
      resume_capacity: Number($("inp-rc").value),
      pause_capacity: Number($("inp-pc").value),
      max_temp: Number($("inp-mt").value),
      resume_temp: Number($("inp-rt").value),
      prioritize_idle: $("inp-idle").checked,
    });
    if (data && data.ok) {
      accLoaded = false;
      loadAccConfig();
    }
  });

  $("btn-bypass-on").addEventListener("click", async () => {
    await apiPost("/api/bypass", { enable: true });
    setTimeout(() => { refresh(); loadAccConfig(); }, 600);
  });

  $("btn-bypass-off").addEventListener("click", async () => {
    await apiPost("/api/bypass", { enable: false });
    setTimeout(() => { refresh(); loadAccConfig(); }, 600);
  });

  $("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const user = $("auth-user").value.trim();
    const pass = $("auth-pass").value;
    const candidate = `Basic ${btoa(`${user}:${pass}`)}`;
    $("auth-err").classList.add("hidden");
    try {
      const res = await fetch("/api/auth/verify", {
        method: "POST",
        headers: { Authorization: candidate },
      });
      if (res.ok) {
        authToken = candidate;
        sessionStorage.setItem("phone_srv_auth", candidate);
        localStorage.setItem("phone_srv_user", user);
        $("auth-pass").value = "";
        updateControlAuthUI();
        toast("控制中心已解锁");
      } else {
        $("auth-err").classList.remove("hidden");
      }
    } catch {
      toast("连接失败，请重试");
    }
  });

  $("btn-logout-ctrl").addEventListener("click", () => {
    lockControlCenter("控制中心已重新锁定");
  });

  $("btn-logout-sms").addEventListener("click", () => {
    lockControlCenter("已锁定退出");
  });

  $("btn-sms-refresh").addEventListener("click", () => {
    loadSmsStatus(false);
    toast("已刷新短信列表");
  });

  $("btn-sms-save").addEventListener("click", async () => {
    const data = await apiPost("/api/sms/config", collectSmsForm());
    if (data && data.status) {
      $("sms-smtp-pass").value = "";
      renderSmsStatus(data.status, true);
    }
  });

  $("btn-sms-test").addEventListener("click", async () => {
    toast("正在发送测试邮件...");
    const data = await apiPost("/api/sms/test", collectSmsForm());
    if (data && data.status) {
      renderSmsStatus(data.status, false);
    }
  });

  $("btn-drop-caches").addEventListener("click", async () => {
    await apiPost("/api/drop-caches");
    setTimeout(refresh, 600);
  });

  document.querySelectorAll("[data-inline-log]").forEach((btn) => {
    btn.addEventListener("click", () => loadInlineLog(btn.dataset.inlineLog));
  });

  document.body.addEventListener("click", async (e) => {
    const otpEl = e.target.closest("[data-copy-otp]");
    if (otpEl) {
      copyText(otpEl.dataset.copyOtp, "验证码");
      return;
    }
    const resendBtn = e.target.closest("[data-resend-sms]");
    if (resendBtn) {
      toast("正在转发短信...");
      const data = await apiPost("/api/sms/resend", { id: Number(resendBtn.dataset.resendSms) });
      if (data && data.status) renderSmsStatus(data.status, false);
      return;
    }
    const btn = e.target.closest("[data-svc]");
    if (!btn) return;
    await apiPost("/api/service", { service: btn.dataset.svc, action: btn.dataset.act });
    setTimeout(refresh, 800);
  });

  let pollTimer = null;
  async function schedulePoll() {
    if (pollTimer) clearTimeout(pollTimer);
    if (!document.hidden) {
      await refresh();
    }
    pollTimer = setTimeout(schedulePoll, 3000);
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) schedulePoll();
  });

  setSort("cpu");
  schedulePoll();
})();
