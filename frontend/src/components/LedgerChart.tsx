import { forwardRef, useMemo, type ComponentProps } from "react";
import ReactECharts from "echarts-for-react/esm/core";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart, SankeyChart } from "echarts/charts";
import {
  AriaComponent, DataZoomComponent, GridComponent, LegendComponent,
  MarkAreaComponent, MarkLineComponent, MarkPointComponent, TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { chartStyle } from "../theme";

// Register only the chart types and interactions used by the app.
echarts.use([
  BarChart, LineChart, PieChart, SankeyChart, CanvasRenderer,
  AriaComponent, DataZoomComponent, GridComponent, LegendComponent,
  MarkAreaComponent, MarkLineComponent, MarkPointComponent, TooltipComponent,
]);

type LedgerChartProps = Omit<ComponentProps<typeof ReactECharts>, "option" | "echarts"> & {
  option: Record<string, unknown>;
};

/** One rendering boundary for the app's chart typography, strokes and controls. */
const LedgerChart = forwardRef<ReactECharts, LedgerChartProps>(function LedgerChart({ option, style, ...props }, ref) {
  const styledOption = useMemo(() => chartStyle.options(option), [option]);
  return <ReactECharts echarts={echarts} {...props} ref={ref} option={styledOption} style={{ width: "100%", minWidth: 0, ...style }} />;
});

export default LedgerChart;
