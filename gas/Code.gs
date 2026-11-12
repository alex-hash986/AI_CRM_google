function onOpen() {
  SpreadsheetApp.getUi().createMenu('🤖 AI CRM')
    .addItem('Пересчитать необработанные лиды', 'scoreUnprocessed')
    .addItem('Симулировать новый лид', 'simulateLead')
    .addItem('Обновить Dashboard', 'refreshDashboard')
    .addToUi();
}

function installCrmTriggers() {
  crmConfig_();
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  const handlers = ['onLeadChange', 'onLeadStructureChange'];
  // Reinstall triggers owned by the account running this function.
  ScriptApp.getProjectTriggers()
    .filter(trigger => handlers.includes(trigger.getHandlerFunction()) &&
      trigger.getTriggerSourceId() === spreadsheet.getId())
    .forEach(trigger => ScriptApp.deleteTrigger(trigger));
  ScriptApp.newTrigger('onLeadChange').forSpreadsheet(spreadsheet).onEdit().create();
  ScriptApp.newTrigger('onLeadStructureChange').forSpreadsheet(spreadsheet).onChange().create();
}

function onLeadChange(e) {
  if (!e || !e.range) return;
  const config = crmConfig_();
  const range = e.range;
  const sheet = range.getSheet();
  if (sheet.getName() !== config.leads || range.getLastRow() < 2) return;
  if (range.getColumn() > 7 || range.getLastColumn() < 3) return;

  const affectsScoring = range.getColumn() <= 7 && range.getLastColumn() >= 5;
  const start = Math.max(2, range.getRow());
  const rows = sheet.getRange(start, 1, range.getLastRow() - start + 1, 7).getValues();
  const failed = [];
  rows.forEach((cells, index) => {
    const newLead = !String(cells[0]).trim();
    if ((!newLead && !affectsScoring) || !cells.slice(2, 7).some(value => value !== '')) return;
    const row = start + index;
    try {
      crmPost_('/score-row', {row_index: row}, config);
    } catch (error) {
      failed.push(row + ' (' + error.message + ')');
    }
  });
  if (failed.length) throw new Error('Не удалось пересчитать строки: ' + failed.join(', '));
}

function onLeadStructureChange(e) {
  if (!e || e.changeType !== 'INSERT_ROW') return;
  // A change event has no edited range. The server scans Leads.
  crmPost_('/score-all');
}

function scoreUnprocessed() {
  menuAction_('/score-all', 'Пакетный скоринг запущен или уже выполняется.');
}

function simulateLead() {
  menuAction_('/simulate-lead', 'Демо-лид добавлен и оценён.');
}

function refreshDashboard() {
  menuAction_('/refresh-dashboard', 'Dashboard обновлён.');
}

function menuAction_(path, success) {
  const ui = SpreadsheetApp.getUi();
  try {
    crmPost_(path);
    ui.alert(success);
  } catch (error) {
    ui.alert(error.message);
  }
}

function crmConfig_() {
  const properties = PropertiesService.getScriptProperties();
  const url = (properties.getProperty('CRM_BASE_URL') || '').trim().replace(/\/+$/, '');
  const secret = properties.getProperty('WEBHOOK_SECRET');
  if (!url.startsWith('https://') || !secret) {
    throw new Error('Укажите CRM_BASE_URL (HTTPS) и WEBHOOK_SECRET в свойствах скрипта.');
  }
  return {url, secret, leads: properties.getProperty('LEADS_SHEET_NAME') || 'Leads'};
}

function crmPost_(path, data, config) {
  config = config || crmConfig_();
  const response = UrlFetchApp.fetch(config.url + path, {
    method: 'post',
    contentType: 'application/json',
    headers: {'X-Webhook-Secret': config.secret},
    payload: JSON.stringify(data || {}),
    muteHttpExceptions: true,
    followRedirects: false,
  });
  const status = response.getResponseCode();
  if (status < 200 || status >= 300) {
    let detail = '';
    try {
      const body = JSON.parse(response.getContentText());
      if (typeof body.detail === 'string') detail = ' — ' + body.detail;
      if (body.detail && body.detail.lead_id) {
        detail = ' — лид ' + body.detail.lead_id + ' уже добавлен. Запустите пакетный скоринг.';
      }
    } catch (_) {
      // Upstream proxies may return HTML.
    }
    throw new Error(path + ': HTTP ' + status + detail);
  }
  return JSON.parse(response.getContentText());
}
