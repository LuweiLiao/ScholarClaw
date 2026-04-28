import type { ProjectScanResult } from '../types';

interface Props {
  scan: ProjectScanResult;
  t: (k: string) => string;
  onStartPlanning?: () => void;
  planningDisabled?: boolean;
  planningHint?: string;
}

export default function ScanResultCard({ scan, t, onStartPlanning, planningDisabled, planningHint }: Props) {
  if (scan.error) {
    return <div className="scan-card scan-error">{scan.error}</div>;
  }

  return (
    <div className="scan-card">
      <h4 className="scan-card-title">{t('scanner.title')}</h4>

      {/* Paper */}
      <div className="scan-section">
        <div className="scan-section-header">{t('scanner.paper')}</div>
        {scan.paper ? (
          <div className="scan-section-body">
            <div className="scan-progress-row">
              <span>{t('scanner.paper_completeness')}</span>
              <div className="scan-progress-bar">
                <div
                  className="scan-progress-fill"
                  style={{ width: `${scan.paper.completeness_pct}%` }}
                />
              </div>
              <span className="scan-pct">{scan.paper.completeness_pct}%</span>
            </div>
            <div className="scan-stats">
              <span>{t('scanner.paper_sections')}: {scan.paper.sections.length}</span>
              <span>{t('scanner.paper_citations')}: {scan.paper.citation_count}</span>
              {scan.paper.empty_sections.length > 0 && (
                <span className="scan-warn">
                  {t('scanner.paper_empty')}: {scan.paper.empty_sections.join(', ')}
                </span>
              )}
            </div>
          </div>
        ) : (
          <div className="scan-section-body scan-empty">{t('scanner.no_paper')}</div>
        )}
      </div>

      {/* Code */}
      <div className="scan-section">
        <div className="scan-section-header">{t('scanner.experiment')}</div>
        {scan.experiment.code_files.length > 0 ? (
          <div className="scan-section-body">
            <div className="scan-stats">
              <span>{t('scanner.experiment_files')}: {scan.experiment.code_files.length}</span>
              <span>{t('scanner.experiment_lines')}: {scan.experiment.total_code_lines}</span>
            </div>
            {scan.experiment.frameworks.length > 0 && (
              <div className="scan-tags">
                {scan.experiment.frameworks.map(f => (
                  <span key={f} className="scan-tag">{f}</span>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="scan-section-body scan-empty">{t('scanner.no_code')}</div>
        )}
      </div>

      {/* Data */}
      <div className="scan-section">
        <div className="scan-section-header">{t('scanner.data')}</div>
        {scan.data.files.length > 0 ? (
          <div className="scan-section-body">
            <div className="scan-stats">
              <span>{t('scanner.data_files')}: {scan.data.files.length}</span>
              <span>{t('scanner.data_size')}: {scan.data.total_size_mb.toFixed(1)} MB</span>
            </div>
          </div>
        ) : (
          <div className="scan-section-body scan-empty">{t('scanner.no_data')}</div>
        )}
      </div>

      {/* Literature */}
      <div className="scan-section">
        <div className="scan-section-header">{t('scanner.literature')}</div>
        <div className="scan-section-body">
          <div className="scan-stats">
            <span>{t('scanner.literature_bib')}: {scan.literature.bib_entry_count}</span>
            <span>{t('scanner.literature_pdf')}: {scan.literature.pdf_count}</span>
          </div>
        </div>
      </div>

      {onStartPlanning && (
        <>
          <button className="scan-plan-btn" onClick={onStartPlanning} disabled={planningDisabled}>
            {t('scanner.start_planning')}
          </button>
          {planningHint && <div className="tex-required-hint">{planningHint}</div>}
        </>
      )}
    </div>
  );
}
