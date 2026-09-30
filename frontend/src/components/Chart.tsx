import {
  AreaSeries,
  CandlestickSeries,
  createChart,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'

export interface Candle {
  time: string
  open: string
  high: string
  low: string
  close: string
}

export interface Point {
  time: string
  value: string
}

type Props =
  | { kind: 'candles'; data: Candle[]; intraday: boolean }
  | { kind: 'area'; data: Point[]; intraday: boolean }

/**
 * The chart labels its axis in UTC. Intraday times are shifted so the labels
 * read as local time, like every other time on the page; daily bars keep their date.
 */
function seconds(iso: string, intraday: boolean): UTCTimestamp {
  const moment = new Date(iso)
  const shift = intraday ? moment.getTimezoneOffset() * 60_000 : 0
  return Math.floor((moment.getTime() - shift) / 1000) as UTCTimestamp
}

/** Keep the last entry per timestamp: the chart needs strictly ascending times. */
function unique<T extends { time: UTCTimestamp }>(rows: T[]): T[] {
  const byTime = new Map<number, T>()
  for (const row of rows) byTime.set(row.time, row)
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

/** TradingView Lightweight Charts, fed from our own history endpoints. */
export default function Chart(props: Props) {
  const container = useRef<HTMLDivElement>(null)
  const { kind, data, intraday } = props

  useEffect(() => {
    if (!container.current) return
    const styles = getComputedStyle(document.documentElement)
    const chart = createChart(container.current, {
      autoSize: true,
      layout: {
        background: { color: 'transparent' },
        textColor: styles.getPropertyValue('--muted').trim(),
      },
      grid: {
        vertLines: { color: styles.getPropertyValue('--line').trim() },
        horzLines: { color: styles.getPropertyValue('--line').trim() },
      },
      timeScale: { timeVisible: intraday, borderVisible: false },
      rightPriceScale: { borderVisible: false },
    })
    if (kind === 'candles') {
      const series = chart.addSeries(CandlestickSeries, {
        upColor: styles.getPropertyValue('--gain').trim(),
        downColor: styles.getPropertyValue('--loss').trim(),
        borderVisible: false,
        wickUpColor: styles.getPropertyValue('--gain').trim(),
        wickDownColor: styles.getPropertyValue('--loss').trim(),
      })
      series.setData(
        unique(
          (data as Candle[]).map((bar) => ({
            time: seconds(bar.time, intraday),
            open: Number(bar.open),
            high: Number(bar.high),
            low: Number(bar.low),
            close: Number(bar.close),
          })),
        ),
      )
    } else {
      const accent = styles.getPropertyValue('--accent').trim()
      const series = chart.addSeries(AreaSeries, {
        lineColor: accent,
        topColor: `${accent}55`,
        bottomColor: `${accent}05`,
        lineWidth: 2,
      })
      series.setData(
        unique(
          (data as Point[]).map((point) => ({
            time: seconds(point.time, intraday),
            value: Number(point.value),
          })),
        ),
      )
    }
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [kind, data, intraday])

  return <div ref={container} className="chart" data-points={data.length} />
}
