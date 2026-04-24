import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/ui/PageHeader';
import { WorkspaceCard } from './settings/WorkspaceCard';
import { LlmCard } from './settings/LlmCard';
import { ConnectorCard } from './settings/ConnectorCard';
import { BehaviourCard } from './settings/BehaviourCard';
import { RehearsalCard } from './settings/RehearsalCard';

/**
 * Settings is just a thin editor on top of the two endpoints that matter:
 *
 *   PATCH /api/workspace           — identity (business name / dump / tone)
 *   PATCH /api/workspace/settings  — everything behaviour-related
 *
 * Each card is its own file under `./settings/`, each driven by the
 * shared `usePatchForm` hook (tracks dirty, resets on workspace update,
 * dispatches the PATCH). The server deep-merges `settings` so we can
 * send partial blobs without clobbering sibling fields. Connector and
 * LLM changes hot-reload server-side — no restart required.
 */
export function Settings() {
  const { data: workspace } = useQuery({
    queryKey: ['workspace'],
    queryFn: () => api.getWorkspace(),
  });

  if (!workspace) {
    return (
      <div className="px-8 py-10 max-w-3xl mx-auto">
        <div className="h-4 w-40 bg-paper-deep animate-pulse mb-4" />
        <div className="h-10 w-72 bg-paper-deep animate-pulse" />
      </div>
    );
  }

  return (
    <div className="px-8 py-10 max-w-3xl mx-auto flex flex-col gap-8">
      <PageHeader
        title="Settings"
        description={
          <>
            Everything here lives in the workspace row and takes effect immediately. No restart,
            no <span className="font-mono">.env</span> to edit. Single-operator install, assumed
            trusted network — keys are stored in plaintext in the DB.
          </>
        }
      />

      <WorkspaceCard workspace={workspace} />
      <LlmCard workspace={workspace} />
      <ConnectorCard workspace={workspace} />
      <BehaviourCard workspace={workspace} />
      <RehearsalCard workspace={workspace} />
    </div>
  );
}
