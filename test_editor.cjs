const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const test = require("node:test");

test("editing nullable fields preserves the declared JSON type", () => {
  const html = readFileSync(__dirname + "/index.html", "utf8");
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const elements = [...html.matchAll(/ id="([^"]+)"/g)].map(([, id]) => ({
    id, dataset: {}, addEventListener() {}
  }));
  runInNewContext(script + `
    parsedJson = {kwota: null, kod: null, liczba: null};
    response = {trace: [
      {field: "kwota", field_type: "number", value: null},
      {field: "kod", field_type: "digits", value: null},
      {field: "liczba", field_type: "integer", value: null}
    ]};
    assert.ok(updateFieldValue("kwota", "-125,50"));
    assert.ok(updateFieldValue("kod", "0012"));
    assert.ok(updateFieldValue("liczba", "12"));
    assert.equal(updateFieldValue("liczba", "1,5"), false);
    assert.equal(updateFieldValue("kwota", "tekst"), false);
    assert.equal(JSON.parse(response.json).kwota, -125.5);
    assert.equal(JSON.parse(response.json).kod, "0012");
    assert.equal(JSON.parse(response.json).liczba, 12);
  `, { assert, document: {querySelectorAll: () => elements}, fetch: async () => ({ok: false}) });
});

test("reset asks for confirmation and applies the returned configuration", async () => {
  const html = readFileSync(__dirname + "/index.html", "utf8");
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const handlers = {};
  const elements = [...html.matchAll(/ id="([^"]+)"/g)].map(([, id]) => ({
    id, dataset: {}, addEventListener(event, fn) { handlers[id + ":" + event] = fn; }
  }));
  const types = [{id: "wlasny"}, {id: "inny", fallback: true}];
  let confirmed = false;
  let resets = 0;
  const context = {
    document: {querySelectorAll: () => elements},
    window: {confirm(message) { assert.match(message, /usunięcia: 1/); return confirmed; }},
    async fetch(url, options) {
      if (url === "/api/config") return {ok: true, json: async () => ({document_types: types})};
      assert.equal(url, "/api/config/reset");
      assert.equal(options.method, "POST");
      assert.equal(options.headers["X-Confirm-Reset"], "yes");
      resets++;
      return {ok: true, json: async () => ({document_types: types.slice(1)})};
    }
  };
  runInNewContext(script, context);
  await handlers["reset-config:click"]();
  assert.equal(resets, 0);
  confirmed = true;
  await handlers["reset-config:click"]();
  assert.equal(resets, 1);
  assert.equal(runInNewContext("labels.wlasny", context), undefined);
  assert.equal(elements.find(e => e.id === "reset-config").disabled, false);
});
