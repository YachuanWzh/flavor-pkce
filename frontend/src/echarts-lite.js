// On-demand ECharts registration (improvement 8): only the charts and
// components the dashboard / agent chat / review pages actually render,
// instead of the full bundle (~1 MB of dead weight for a tree-shaken
// build). Add here first when a new option type appears.
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
]);

export default echarts;
