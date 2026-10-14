/* Chat box used on the public home page (anonymous) and in the customer portal (logged in).
 * <div id="chatbox" data-mode="web|portal" data-greeting="..."></div>
 */
(function () {
  var box = document.getElementById("chatbox");
  if (!box) return;
  var mode = box.dataset.mode;
  var sid = "";
  if (mode === "web") {
    try { sid = localStorage.getItem("shk_chat_sid") || ""; } catch (e) {}
    if (!sid) {
      sid = "w" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      try { localStorage.setItem("shk_chat_sid", sid); } catch (e) {}
    }
  }
  var sendUrl = mode === "web" ? "/api/webchat" : "/portal/chat";
  var histUrl = mode === "web" ? "/api/webchat/history?session_id=" + encodeURIComponent(sid) + "&after=" : "/portal/chat/history?after=";

  box.innerHTML = '<div class="cb-log" aria-live="polite"></div>' +
    '<form class="cb-form"><input class="cb-in" maxlength="1000" autocomplete="off" placeholder="e.g. 50 MT sponge iron to Nagpur"><button aria-label="Send">Send ➤</button></form>';
  var log = box.querySelector(".cb-log"), form = box.querySelector(".cb-form"), input = box.querySelector(".cb-in");
  var lastId = 0, busy = false;

  function add(who, text, files, at) {
    var d = document.createElement("div");
    d.className = "cb-m cb-" + who;
    d.textContent = text;
    if (who === "staff") { var tag = document.createElement("small"); tag.textContent = "SHK sales team"; d.prepend(tag); }
    (files || []).forEach(function (f) {
      var a = document.createElement("a"); a.href = f.url; a.target = "_blank"; a.className = "cb-file";
      a.textContent = "📄 " + f.label; d.appendChild(a);
    });
    if (at) { var t = document.createElement("time"); t.textContent = at; d.appendChild(t); }
    log.appendChild(d); log.scrollTop = log.scrollHeight;
    return d;
  }

  function poll() {
    fetch(histUrl + lastId, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { messages: [] }; })
      .then(function (d) {
        d.messages.forEach(function (m) { add(m.from, m.text, null, m.at); lastId = Math.max(lastId, m.id); });
        if (!lastId && !log.children.length && box.dataset.greeting) add("ai", box.dataset.greeting);
      }).catch(function () {});
  }

  form.onsubmit = function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text || busy) return;
    busy = true; input.value = "";
    add("me", text);
    var wait = add("ai", "…"); wait.classList.add("cb-wait");
    fetch(sendUrl, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
                     body: JSON.stringify({ session_id: sid, message: text }) })
      .then(function (r) {
        if (r.status === 401) { location.href = "/portal/login"; throw new Error("login"); }
        return r.json();
      })
      .then(function (d) {
        log.removeChild(wait);
        if (d.reply) add("ai", d.reply, d.files);
        lastId = Math.max(lastId, d.last_id || 0);
      })
      .catch(function () { wait.textContent = "Connection problem. Please try again or call 0771-264984."; })
      .finally(function () { busy = false; input.focus(); });
  };

  poll();
  setInterval(function () { if (!busy && !document.hidden) poll(); }, 15000);  // picks up replies typed by staff

  // Buttons/links with data-ask="question" anywhere on the page: scroll to the chat and ask it.
  // An empty data-ask just brings the chat into view and focuses the input.
  document.querySelectorAll("[data-ask]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      e.preventDefault();
      var card = document.getElementById("chat") || box;
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      var q = el.getAttribute("data-ask");
      if (q) { input.value = q; form.requestSubmit ? form.requestSubmit() : form.onsubmit(new Event("submit")); }
      setTimeout(function () { input.focus({ preventScroll: true }); }, 400);
    });
  });
})();
