// Keep in sync with the body and Tailwind system-font stack.
export const FONT_FAMILY = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif';

// Am I Broke?: warm paper, graphite and a small amount of terracotta.
export const C = {
  abyss: "#202522",
  deep: "#F5F4EF",
  surface: "#FFFFFF",
  surface2: "#ECEEE8",
  line: "#D8DDD5",
  foam: "#252C28",
  mist: "#606960",
  teal: "#AF4321",
  cyan: "#477486",
  seablue: "#536FA3",
  kelp: "#277253",
  coral: "#B54747",
  amber: "#96631B",
};
const ACCOUNT_RAMP = [
  C.teal,
  C.seablue,
  C.cyan,
  C.kelp,
  C.amber,
  "#887393",
  C.coral,
];
export function accountColor(name: string, _index = 0): string {
  const bank = name.toLowerCase();
  if (bank.includes("amex") || bank.includes("american express")) return C.cyan;
  if (bank.includes("hsbc")) return C.teal;
  if (bank.includes("lloyds")) return C.kelp;
  if (bank.includes("revolut")) return C.seablue;
  if (bank.replace(/\s/g, "").includes("trading212")) return "#887393";
  const hash = [...name].reduce(
    (h, ch) => (h * 31 + ch.charCodeAt(0)) >>> 0,
    0,
  );
  return ACCOUNT_RAMP[hash % ACCOUNT_RAMP.length];
}
export const axisBase = {
  axisLine: { onZero: true, lineStyle: { color: C.line, type: "solid" } },
  axisTick: { show: false },
  axisLabel: { color: C.mist, fontSize: 11, hideOverlap: true },
  splitLine: { lineStyle: { color: "#E7EAE4", width: 1, type: "solid" } },
};
export const tooltipBase = {
  confine: true,
  enterable: true,
  backgroundColor: "rgba(255,255,255,0.98)",
  borderColor: C.line,
  borderWidth: 1,
  textStyle: {
    color: C.foam,
    fontSize: 12,
    fontFamily: FONT_FAMILY,
  },
  extraCssText: "box-shadow:0 8px 24px rgba(25,35,28,.10);border-radius:8px;max-width:min(360px,calc(100vw - 32px));max-height:min(60vh,300px);overflow:auto;white-space:normal;overflow-wrap:anywhere;",
  axisPointer: { lineStyle: { color: C.mist, width: 1, type: "solid" }, crossStyle: { color: C.mist, width: 1, type: "solid" } },
};

type ChartOption = Record<string, unknown>;
const objectOption = (value: unknown): ChartOption =>
  value && typeof value === "object" && !Array.isArray(value) ? value as ChartOption : {};
function mapOptions(value: unknown, map: (option: ChartOption) => ChartOption): unknown {
  return Array.isArray(value) ? value.map((item) => map(objectOption(item))) : map(objectOption(value));
}

/** Shared visual policy. Charts retain their data, units, scale, gaps and layout. */
export class ChartStylePolicy {
  readonly fontFamily = FONT_FAMILY;
  readonly lineWidth = 2;
  readonly markerSize = 5;
  readonly gridLineColor = "#E7EAE4";
  readonly zeroLineColor = C.line;
  readonly legend = {
    type: "scroll", left: 0, right: 0, top: 0,
    itemWidth: 14, itemHeight: 10, itemGap: 12, pageIconSize: 10,
    textStyle: { color: C.mist, fontSize: 11, fontFamily: this.fontFamily, overflow: "truncate" },
    tooltip: { show: true },
  };

  private axis(option: ChartOption, partial: boolean): ChartOption {
    if (partial) return {
      ...option,
      ...(option.axisLabel ? { axisLabel: { ...objectOption(option.axisLabel), fontSize: 11, fontFamily: this.fontFamily } } : {}),
      ...(option.axisLine ? { axisLine: { ...objectOption(option.axisLine), lineStyle: { ...objectOption(objectOption(option.axisLine).lineStyle), color: this.zeroLineColor, type: "solid" } } } : {}),
      ...(option.type !== undefined || option.splitLine ? { splitLine: {
        ...(option.type !== undefined ? { show: option.type === "value" || option.type === "log" } : {}),
        ...objectOption(option.splitLine),
        lineStyle: { ...objectOption(objectOption(option.splitLine).lineStyle), color: this.gridLineColor, width: 1, type: "solid" },
      } } : {}),
    };
    const axisLine = objectOption(option.axisLine);
    const splitLine = objectOption(option.splitLine);
    return {
      ...axisBase, ...option,
      axisLine: { ...axisBase.axisLine, ...axisLine, lineStyle: { ...objectOption(axisLine.lineStyle), color: this.zeroLineColor, type: "solid" } },
      axisLabel: { ...axisBase.axisLabel, ...objectOption(option.axisLabel), color: C.mist, fontSize: 11, fontFamily: this.fontFamily },
      splitLine: { show: option.type === "value" || option.type === "log", ...splitLine, lineStyle: { ...objectOption(splitLine.lineStyle), color: this.gridLineColor, width: 1, type: "solid" } },
    };
  }

  private series(option: ChartOption): ChartOption {
    const result = { ...option };
    if (option.type === "line") {
      Object.assign(result, {
        symbol: "circle", showSymbol: false,
        ...option, smooth: false, connectNulls: false,
        symbolSize: typeof option.symbolSize === "number" ? Math.max(this.markerSize, option.symbolSize) : option.symbolSize ?? this.markerSize,
        lineStyle: { ...objectOption(option.lineStyle), width: option.stack ? 1.5 : this.lineWidth, type: option.preserveLinePattern === true ? objectOption(option.lineStyle).type ?? "solid" : "solid", cap: "round", join: "round" },
      });
    }
    // Opt in when a line pattern conveys a different valuation basis.
    delete result.preserveLinePattern;
    if (option.markLine) {
      const markLine = objectOption(option.markLine);
      result.markLine = { ...markLine, lineStyle: { color: C.mist, width: 1, ...objectOption(markLine.lineStyle), type: "solid" } };
    }
    return result;
  }

  private fragment(option: ChartOption, partial = false): ChartOption {
    const result = { ...option };
    for (const key of ["xAxis", "yAxis"]) {
      if (option[key] !== undefined) result[key] = mapOptions(option[key], (axis) => this.axis(axis, partial));
    }
    if (option.series !== undefined) result.series = mapOptions(option.series, (series) => this.series(series));
    if (option.legend !== undefined) result.legend = mapOptions(option.legend, (legend) => ({
      ...(!partial ? this.legend : {}), ...legend,
      textStyle: { ...(!partial ? this.legend.textStyle : {}), ...objectOption(legend.textStyle), fontFamily: this.fontFamily, fontSize: 11 },
    }));
    if (option.tooltip !== undefined) result.tooltip = mapOptions(option.tooltip, (tooltip) => ({
      ...tooltipBase, ...tooltip,
      textStyle: { ...tooltipBase.textStyle, ...objectOption(tooltip.textStyle), fontFamily: this.fontFamily },
      axisPointer: { ...tooltipBase.axisPointer, ...objectOption(tooltip.axisPointer) },
    }));
    if (option.dataZoom !== undefined) result.dataZoom = mapOptions(option.dataZoom, (zoom) => zoom.type === "slider" ? {
      ...zoom, height: zoom.height ?? 18, bottom: zoom.bottom ?? 8, borderColor: C.line, backgroundColor: "transparent",
      fillerColor: "rgba(175,67,33,0.10)", handleStyle: { color: C.teal },
      moveHandleStyle: { color: C.teal, opacity: 0.25 },
      dataBackground: { lineStyle: { color: C.mist, type: "solid" }, areaStyle: { color: C.line } },
      selectedDataBackground: { lineStyle: { color: C.teal, type: "solid" }, areaStyle: { color: "rgba(175,67,33,0.15)" } },
      textStyle: { color: C.mist, fontSize: 11, fontFamily: this.fontFamily },
    } : zoom);
    return result;
  }

  options(option: ChartOption): ChartOption {
    if (option.baseOption) return {
      ...option,
      baseOption: this.options(objectOption(option.baseOption)),
      // Media entries are partial overrides, so do not invent missing series or axes.
      ...(Array.isArray(option.media) ? { media: option.media.map((entry) => {
        const media = objectOption(entry);
        return { ...media, option: this.fragment(objectOption(media.option), true) };
      }) } : {}),
    };
    return {
      ...this.fragment(option),
      animation: false, backgroundColor: "transparent",
      aria: { enabled: true, ...objectOption(option.aria) },
      textStyle: { color: C.foam, fontSize: 12, ...objectOption(option.textStyle), fontFamily: this.fontFamily },
    };
  }
}

export const chartStyle = new ChartStylePolicy();
