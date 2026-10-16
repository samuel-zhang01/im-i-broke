import assert from "node:assert/strict";
import test from "node:test";
import * as echarts from "echarts";
import { sourceModule } from "./source-module.mjs";

const { chartStyle, FONT_FAMILY } = await sourceModule("theme");

test("shared chart styling preserves financial data, labels and missing observations", () => {
  const values = Object.freeze([100, null, 0, -20]);
  const formatter = (value) => `GBP ${value}`;
  const lineStyle = Object.freeze({ type: "dashed", color: "#123456" });
  const option = Object.freeze({
    yAxis: { type: "value", scale: true, axisLabel: { formatter } },
    tooltip: { formatter },
    series: Object.freeze([{ type: "line", step: "end", data: values, lineStyle, symbol: "diamond", showSymbol: true }]),
  });
  const styled = chartStyle.options(option);
  assert.equal(styled.series[0].data, values);
  assert.equal(styled.series[0].step, "end");
  assert.equal(styled.series[0].connectNulls, false);
  assert.equal(styled.series[0].symbol, "diamond");
  assert.equal(styled.series[0].showSymbol, true);
  assert.equal(styled.yAxis.scale, true);
  assert.equal(styled.yAxis.axisLabel.formatter, formatter);
  assert.equal(styled.tooltip.formatter, formatter);
  assert.equal(lineStyle.type, "dashed");
  assert.equal(styled.series[0].lineStyle.type, "solid");
  assert.equal(styled.series[0].lineStyle.color, "#123456");
  assert.equal(styled.textStyle.fontFamily, FONT_FAMILY);
});

test("responsive partial options keep axis orientation, baseline and data intact", () => {
  const data = [5, -10];
  const option = chartStyle.options({
    baseOption: { xAxis: { type: "category", data: ["A", "B"] }, yAxis: { type: "value" }, series: [{ type: "bar", data }] },
    media: [
      { query: { maxWidth: 480 }, option: { xAxis: { type: "value", data: [] }, yAxis: { type: "category", data: ["A", "B"] }, series: [{ data }] } },
      { option: { xAxis: { type: "category", data: ["A", "B"] }, yAxis: { type: "value", data: [] } } },
    ],
  });
  assert.equal(option.media[0].option.series[0].data, data);
  assert.equal(option.media[0].option.series[0].type, undefined);
  assert.equal(option.media[0].option.yAxis.splitLine.show, false);
  assert.equal(option.media[0].option.xAxis.splitLine.show, true);
  const chart = echarts.init(null, null, { renderer: "svg", ssr: true, width: 390, height: 260 });
  try {
    chart.setOption(option);
    assert.equal(chart.getOption().xAxis[0].type, "value");
    assert.equal(chart.getOption().yAxis[0].type, "category");
    assert.equal(chart.getOption().xAxis[0].splitLine.show, true);
    assert.equal(chart.getOption().yAxis[0].splitLine.show, false);
    assert.deepEqual(chart.getOption().series[0].data, data);
    chart.resize({ width: 900 });
    assert.equal(chart.getOption().xAxis[0].type, "category");
    assert.equal(chart.getOption().yAxis[0].type, "value");
    assert.equal(chart.getOption().xAxis[0].splitLine.show, false);
    assert.equal(chart.getOption().yAxis[0].splitLine.show, true);
    assert.deepEqual(chart.getOption().series[0].data, data);
  } finally {
    chart.dispose();
  }
});

test("line and cost-reference rendering use solid strokes with visible isolated values", () => {
  const chart = echarts.init(null, null, { renderer: "svg", ssr: true, width: 500, height: 260 });
  try {
    chart.setOption(chartStyle.options({
      xAxis: { type: "category", data: ["A", "B", "C", "D"] },
      yAxis: { type: "value" },
      series: [{
        type: "line", data: [10, null, -5, 0], showSymbol: true,
        lineStyle: { type: "dotted" },
        markLine: { symbol: "none", lineStyle: { type: "dashed" }, data: [{ yAxis: 5 }] },
      }],
    }));
    const rendered = chart.renderToSVGString();
    assert.doesNotMatch(rendered, /stroke-dasharray="[^"]+"/);
    assert.equal(chart.getOption().series[0].connectNulls, false);
    assert.equal(chart.getOption().series[0].showSymbol, true);
    assert.match(rendered, /<path/);
  } finally {
    chart.dispose();
  }
});

test("an estimated comparison can preserve its semantic dashed pattern", () => {
  const chart = echarts.init(null, null, { renderer: "svg", ssr: true, width: 500, height: 260 });
  try {
    const styled = chartStyle.options({
      xAxis: { type: "category", data: ["A", "B", "C"] }, yAxis: { type: "value" },
      series: [{ type: "line", data: [10, 15, 12], preserveLinePattern: true, lineStyle: { type: "dashed" } }],
    });
    assert.equal(styled.series[0].preserveLinePattern, undefined);
    chart.setOption(styled);
    assert.match(chart.renderToSVGString(), /stroke-dasharray="[^"]+"/);
  } finally { chart.dispose(); }
});
