import { getWeb } from './webApi'
import type { BacktestDetail, BacktestTable } from '../types/backtests'
export const backtestApi = {
  detail: (id = 'latest') => getWeb<BacktestDetail>(`/api/v1/web/backtests/${id}`),
  equity: (id = 'latest') => getWeb<BacktestTable>(`/api/v1/web/backtests/${id}/equity`),
  trades: (id = 'latest') => getWeb<BacktestTable>(`/api/v1/web/backtests/${id}/trades`),
}
