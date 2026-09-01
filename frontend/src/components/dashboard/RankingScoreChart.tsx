import { ChartCard } from '../common/ChartCard'
import { SimpleLineChart } from '../common/SimpleLineChart'
import type { RankingRecord } from '../../types/dashboard'

export function RankingScoreChart({ records }: { records: RankingRecord[] }) {
  const points = records
    .slice(0, 50)
    .map((record, index) => ({
      x: String(record.code ?? index),
      y: Number(record.model_score ?? record.pred_score ?? record.score),
    }))
    .filter((point) => Number.isFinite(point.y))
    .map((point) => ({ ...point, y: point.y * 100 }))

  return <ChartCard title="前50名融合排序分（百分制）"><SimpleLineChart points={points} ariaLabel="前50名融合排序分" /></ChartCard>
}
