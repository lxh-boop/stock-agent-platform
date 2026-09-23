import { getWeb } from './webApi'
import type { DashboardSummary, FreshnessItem, RankingPageData } from '../types/dashboard'
export const dashboardApi = {
  summary: () => getWeb<DashboardSummary>('/api/v1/web/dashboard/summary'),
  rankings: (offset = 0, limit = 100) => getWeb<RankingPageData>('/api/v1/web/dashboard/rankings', { params: { offset, limit } }),
  dataFreshness: () => getWeb<FreshnessItem[]>('/api/v1/web/dashboard/data-freshness'),
}
