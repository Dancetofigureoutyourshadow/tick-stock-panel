import { BrainCircuit } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { ChanTrainingPage } from './page'

const extension: FrontendExtension = {
  id: 'chan.training',
  apiVersion: 1,
  routes: [
    { id: 'chan-training', path: '/chan-training', component: ChanTrainingPage },
  ],
  navigation: [
    { id: 'chan-training', routeId: 'chan-training', label: '缠论训练', icon: BrainCircuit, order: 150 },
  ],
}

export default extension
