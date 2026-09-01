import { Trophy } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { DragonTigerPage } from './page'

const extension: FrontendExtension = {
  id: 'market.dragon-tiger',
  apiVersion: 1,
  routes: [
    { id: 'dragon-tiger', path: '/dragon-tiger', component: DragonTigerPage },
  ],
  navigation: [
    { id: 'dragon-tiger', routeId: 'dragon-tiger', label: '龙虎榜', icon: Trophy, order: 160 },
  ],
}

export default extension
