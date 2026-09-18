import { Eye } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { BlindTrainingPage } from './page'

const extension: FrontendExtension = {
  id: 'blind.training',
  apiVersion: 1,
  routes: [
    { id: 'blind-training', path: '/blind-training', component: BlindTrainingPage },
  ],
  navigation: [
    { id: 'blind-training', routeId: 'blind-training', label: '盲测训练', icon: Eye, order: 150 },
  ],
}

export default extension
