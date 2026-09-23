import { getWeb } from './webApi'
import type { NewsEventsData } from '../types/news'
export interface NewsFilters { stock_code?: string; event_type?: string; start_date?: string; end_date?: string; offset?: number; limit?: number }
export const newsApi = {
  events: (filters: NewsFilters = {}) => getWeb<NewsEventsData>('/api/v1/web/news/events', { params: filters }),
}
