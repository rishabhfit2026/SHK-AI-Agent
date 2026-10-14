/* SHK website chat widget.
 * Add to shkraipur.com (before </body>):
 *   <script src="https://YOUR-SALES-DESK-HOST/static/widget.js" defer></script>
 */
(function () {
  var script = document.currentScript || document.querySelector('script[src*="widget.js"]');
  var API = new URL(script.src).origin;
  var sid;
  try { sid = localStorage.getItem("shk_chat_sid"); } catch (e) {}
  if (!sid) {
    sid = "w" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    try { localStorage.setItem("shk_chat_sid", sid); } catch (e) {}
  }

  var css = "#shk-btn{position:fixed;right:18px;bottom:18px;z-index:99999;background:#1f3b57;color:#fff;border:0;border-radius:28px;padding:12px 18px;font:600 14px system-ui;box-shadow:0 4px 14px rgba(0,0,0,.25);cursor:pointer}" +
    "#shk-box{position:fixed;right:18px;bottom:76px;z-index:99999;width:min(360px,calc(100vw - 36px));height:min(520px,calc(100vh - 110px));background:#fff;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden;font:14px/1.4 system-ui}" +
    "#shk-box header{background:#1f3b57;color:#fff;padding:12px 14px;font-weight:600}#shk-box header small{display:block;font-weight:400;opacity:.8}" +
    "#shk-log{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:8px;background:#f5f6f8}" +
    ".shk-m{max-width:85%;padding:8px 10px;border-radius:10px;white-space:pre-wrap;word-wrap:break-word}.shk-u{align-self:flex-end;background:#dcf3e4}.shk-a{align-self:flex-start;background:#fff;border:1px solid #e4e7ec}" +
    "#shk-f{display:flex;border-top:1px solid #e4e7ec}#shk-in{flex:1;border:0;padding:12px;font:inherit;outline:none}#shk-f button{border:0;background:#1f3b57;color:#fff;padding:0 16px;font:inherit;cursor:pointer}";
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  var btn = document.createElement("button"); btn.id = "shk-btn"; btn.textContent = "💬 Get today's rate";
  var box = document.createElement("div"); box.id = "shk-box";
  box.innerHTML = '<header>Shri Hare-Krishna Sponge Iron<small>Sponge iron · Billets · Sections · Steel shots · <a href="' + API + '/portal/login" target="_blank" style="color:#fff">Customer login</a></small></header>' +
    '<div id="shk-log"></div><form id="shk-f"><input id="shk-in" placeholder="Type your requirement…" maxlength="1000" autocomplete="off"><button>Send</button></form>';
  document.body.appendChild(btn); document.body.appendChild(box);
  var log = box.querySelector("#shk-log"), form = box.querySelector("#shk-f"), input = box.querySelector("#shk-in");

  function add(text, who, files) {
    var d = document.createElement("div"); d.className = "shk-m " + (who === "u" ? "shk-u" : "shk-a"); d.textContent = text;
    if (who === "staff") { d.style.borderColor = "#93c5fd"; d.textContent = "SHK sales team: " + text; }
    (files || []).forEach(function (f) {
      var a = document.createElement("a"); a.href = API + f.url; a.target = "_blank"; a.textContent = "\n📄 " + f.label;
      d.appendChild(a);
    });
    log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
  }
  var lastId = 0, busy = false, loaded = false;
  function poll() {
    fetch(API + "/api/webchat/history?session_id=" + encodeURIComponent(sid) + "&after=" + lastId)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        d.messages.forEach(function (m) { add(m.text, m.from === "me" ? "u" : m.from); lastId = Math.max(lastId, m.id); });
        if (!loaded && !lastId) add("Namaste! Tell me the product, quantity and delivery city and I'll get you today's rate.", "a");
        loaded = true;
      }).catch(function () {});
  }
  btn.onclick = function () {
    var open = box.style.display === "flex"; box.style.display = open ? "none" : "flex";
    if (!open && !loaded) poll();
    if (!open) input.focus();
  };
  setInterval(function () { if (loaded && !busy && box.style.display === "flex") poll(); }, 15000);
  form.onsubmit = function (e) {
    e.preventDefault(); var text = input.value.trim(); if (!text) return;
    if (busy) return;
    busy = true; input.value = ""; add(text, "u"); var wait = add("…", "a");
    fetch(API + "/api/webchat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sid, message: text }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        log.removeChild(wait); add(d.reply || "Sorry, please call 0771-264984.", "a", d.files);
        lastId = Math.max(lastId, d.last_id || 0);
      })
      .catch(function () { wait.textContent = "Connection problem. Please call 0771-264984."; })
      .finally(function () { busy = false; });
  };
})();
