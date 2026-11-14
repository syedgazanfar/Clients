/* HeyStartup lead widget.
   Usage: <script src="https://YOUR-LEADFLOW-HOST/widget.js" defer></script>
   Optional attributes on the script tag:
     data-whatsapp="447700900123"  adds a "Chat on WhatsApp" link to your number
     data-color="#0b5fff"          accent colour
     data-label="Talk to us"       button text */
(function () {
  var script = document.currentScript;
  var api = new URL(script.src).origin;
  var color = script.getAttribute("data-color") || "#128c7e";
  var label = script.getAttribute("data-label") || "Talk to us on WhatsApp";
  var number = (script.getAttribute("data-whatsapp") || "").replace(/\D/g, "");

  var host = document.createElement("div");
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML =
    "<style>" +
    ":host{all:initial}*{box-sizing:border-box;font-family:system-ui,sans-serif}" +
    ".btn{position:fixed;right:20px;bottom:20px;z-index:2147483000;background:" + color +
    ";color:#fff;border:0;border-radius:28px;padding:14px 20px;font-size:15px;font-weight:600;" +
    "cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25)}" +
    ".panel{position:fixed;right:20px;bottom:80px;z-index:2147483000;width:340px;" +
    "max-width:calc(100vw - 32px);max-height:calc(100vh - 110px);overflow:auto;background:#fff;" +
    "color:#1b1f24;border-radius:12px;padding:18px;box-shadow:0 8px 30px rgba(0,0,0,.25);display:none}" +
    ".panel.open{display:block}h3{margin:0 0 4px;font-size:17px}p{margin:0 0 12px;font-size:13px;color:#5b6570}" +
    "label{display:block;font-size:12px;font-weight:600;margin:8px 0 3px}" +
    "input,select,textarea{width:100%;padding:8px 10px;border:1px solid #cfd5dc;border-radius:6px;font-size:14px}" +
    ".consent{display:flex;gap:8px;align-items:flex-start;font-weight:400;font-size:12px;color:#5b6570;margin-top:12px}" +
    ".consent input{width:auto;margin-top:2px}.hp{position:absolute;left:-9999px}" +
    ".send{width:100%;margin-top:12px;background:" + color + ";color:#fff;border:0;border-radius:6px;" +
    "padding:11px;font-size:15px;font-weight:600;cursor:pointer}.send:disabled{opacity:.6}" +
    ".note{margin-top:10px;font-size:13px}.err{color:#c62828}.alt{display:block;margin-top:10px;font-size:13px;color:" + color + "}" +
    "</style>" +
    '<button class="btn" type="button" aria-expanded="false">' + label + "</button>" +
    '<div class="panel" role="dialog" aria-label="Contact form">' +
    "<h3>Start a conversation</h3>" +
    "<p>Leave your details and we will message you on WhatsApp straight away.</p>" +
    "<form>" +
    '<label for="n">Name</label><input id="n" name="name" required maxlength="120" autocomplete="name">' +
    '<label for="p">WhatsApp number (with country code)</label>' +
    '<input id="p" name="phone" type="tel" required maxlength="30" placeholder="+44 7700 900123" autocomplete="tel">' +
    '<label for="e">Work email</label><input id="e" name="email" type="email" maxlength="200" autocomplete="email">' +
    '<label for="c">Company</label><input id="c" name="company" maxlength="200" autocomplete="organization">' +
    '<label for="s">What do you need?</label><select id="s" name="service">' +
    '<option value="">Choose one</option>' +
    "<option>AI chatbots and assistants</option>" +
    "<option>AI integration with existing software</option>" +
    "<option>Custom AI model development</option>" +
    "<option>AI automation and workflow</option>" +
    "<option>Not sure yet</option></select>" +
    '<label for="m">Anything else?</label><textarea id="m" name="message" rows="2" maxlength="1000"></textarea>' +
    '<div class="hp" aria-hidden="true"><input name="website" tabindex="-1" autocomplete="off"></div>' +
    '<label class="consent"><input type="checkbox" name="consent" required>' +
    "<span>I agree to be contacted on WhatsApp about my enquiry. Reply STOP at any time to opt out.</span></label>" +
    '<button class="send" type="submit">Send</button>' +
    '<div class="note" role="status"></div>' +
    "</form>" +
    (number
      ? '<a class="alt" target="_blank" rel="noopener" href="https://wa.me/' + number +
        '?text=' + encodeURIComponent("Hi, I would like to talk about an AI project.") +
        '">Or message us directly on WhatsApp</a>'
      : "") +
    "</div>";

  var button = root.querySelector(".btn");
  var panel = root.querySelector(".panel");
  var form = root.querySelector("form");
  var note = root.querySelector(".note");
  var send = root.querySelector(".send");

  button.addEventListener("click", function () {
    var open = panel.classList.toggle("open");
    button.setAttribute("aria-expanded", String(open));
    if (open) root.querySelector("#n").focus();
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var data = Object.fromEntries(new FormData(form).entries());
    data.consent = form.elements.consent.checked;
    send.disabled = true;
    note.className = "note";
    note.textContent = "Sending...";
    fetch(api + "/api/leads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok) {
            throw new Error(typeof body.detail === "string" ? body.detail : "Please check the form and try again.");
          }
          form.reset();
          note.textContent = "Thanks. Check WhatsApp, we have just messaged you.";
        });
      })
      .catch(function (error) {
        note.className = "note err";
        note.textContent = error.message || "Something went wrong. Please try again.";
      })
      .finally(function () { send.disabled = false; });
  });
})();
