/**
 * Payrolla mail relay: lets payrollagh.com send email from your Gmail over HTTPS.
 * (Render's free plan blocks SMTP, so the app posts here and MailApp sends.)
 *
 * Setup, once, signed in as the Gmail account that should send:
 *  1. script.google.com -> New project -> replace Code.gs with this file -> Save.
 *  2. Project Settings (gear) -> Script properties -> Add:
 *       RELAY_SECRET = a long random string (the same value goes into Render as
 *       APPS_SCRIPT_MAIL_SECRET).
 *  3. Deploy -> New deployment -> type "Web app":
 *       Execute as: Me      Who has access: Anyone
 *     Authorise when asked ("Google hasn't verified this app" -> Advanced -> Go to ...).
 *  4. Copy the Web app URL (ends in /exec) into Render as APPS_SCRIPT_MAIL_URL.
 *  After editing this file later: Deploy -> Manage deployments -> Edit -> New version,
 *  so the URL stays the same.
 *
 * Limits (Google's, personal account): 100 recipients/day, 50 per message,
 * 25 MB of attachments per message.
 */

function doPost(e) {
  try {
    var data = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var expected = PropertiesService.getScriptProperties().getProperty('RELAY_SECRET');
    if (!expected || !safeEquals_(String(data.secret || ''), expected)) {
      return reply_({ ok: false, error: 'unauthorised' });
    }
    if (!data.to || !data.subject || !(data.text || data.html)) {
      return reply_({ ok: false, error: 'to, subject and a body are required' });
    }
    if (String(data.to).indexOf(',') !== -1 || String(data.to).indexOf(';') !== -1) {
      return reply_({ ok: false, error: 'one recipient per message' });
    }
    if (MailApp.getRemainingDailyQuota() < 1) {
      return reply_({ ok: false, error: 'daily email quota reached; try again tomorrow' });
    }

    var options = {
      to: String(data.to),
      subject: String(data.subject),
      body: String(data.text || ''),
      name: String(data.name || 'Payrolla')
    };
    if (data.html) options.htmlBody = String(data.html);
    if (data.replyTo) options.replyTo = String(data.replyTo);
    if (data.attachments && data.attachments.length) {
      options.attachments = data.attachments.map(function (a) {
        return Utilities.newBlob(Utilities.base64Decode(a.content),
                                 a.mimeType || 'application/octet-stream', a.filename);
      });
    }
    MailApp.sendEmail(options);
    return reply_({ ok: true, remaining: MailApp.getRemainingDailyQuota() });
  } catch (err) {
    return reply_({ ok: false, error: String(err && err.message || err) });
  }
}

function doGet() {
  return reply_({ ok: true, service: 'payrolla-mail-relay' });
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// Constant-time comparison, so the secret can't be guessed a character at a time.
function safeEquals_(a, b) {
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
