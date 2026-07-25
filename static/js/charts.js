    // ── Charts ─────────────────────────────────────────────────────────────────
    const CHART_DEFAULTS = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          labels: { color: '#9d9da8', font: { family: 'Inter', size: 11 }, boxWidth: 8, padding: 14, usePointStyle: true, pointStyle: 'circle' }
        }
      }
    };

    function initCharts() {
      chartVerify = new Chart(document.getElementById('chartVerify'), {
        type: 'bar',
        data: {
          labels: ['Valid', 'Invalid', 'Catch-all', 'Other'],
          datasets: [{
            data: [0, 0, 0, 0],
            backgroundColor: ['rgba(16,185,129,.75)','rgba(239,68,68,.75)','rgba(245,158,11,.75)','rgba(99,99,110,.55)'],
            borderRadius: 6, borderSkipped: false,
          }]
        },
        options: {
          ...CHART_DEFAULTS,
          plugins: {
            legend: { display: false, labels: { color: '#9d9da8' } }
          },
          scales: {
            x: { ticks: { color: '#63636e', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' } },
            y: { ticks: { color: '#63636e', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' }, beginAtZero: true }
          }
        }
      });

      // All-time verification outcome, for the Home dashboard — distinct
      // from chartVerify above (which is scoped to the currently-loaded
      // single run). Fed by GET /api/history/aggregate via
      // updateHomeCharts(), never by the per-run SSE 'stats'/'complete'
      // events chartVerify listens to.
      const donutEl = document.getElementById('chartOutcomeDonut');
      if (donutEl) {
        chartOutcomeDonut = new Chart(donutEl, {
          type: 'doughnut',
          data: {
            labels: ['Valid', 'Invalid', 'Catch-all'],
            datasets: [{
              data: [0, 0, 0],
              backgroundColor: ['rgba(16,185,129,.75)', 'rgba(239,68,68,.75)', 'rgba(245,158,11,.75)'],
              borderWidth: 0,
            }]
          },
          options: {
            ...CHART_DEFAULTS,
            cutout: '65%',
          }
        });
      }
    }

    function updateCharts(stats) {
      if (chartVerify && stats.verified !== undefined) {
        const total = stats.deduped || stats.verified || 1;
        const other = total - (stats.verified || 0) - (stats.invalid || 0) - (stats.catchall || 0);
        chartVerify.data.datasets[0].data = [
          stats.verified || 0,
          stats.invalid  || 0,
          stats.catchall || 0,
          Math.max(0, other)
        ];
        chartVerify.update();
      }
    }

    // outcomeTotals: {valid, invalid, catchall} — see
    // app/blueprints/history.py's aggregate_stats(). Called from
    // static/js/home-dashboard.js's loadHomeDashboard().
    function updateHomeCharts(outcomeTotals) {
      if (!chartOutcomeDonut) return;
      chartOutcomeDonut.data.datasets[0].data = [
        outcomeTotals.valid    || 0,
        outcomeTotals.invalid  || 0,
        outcomeTotals.catchall || 0,
      ];
      chartOutcomeDonut.update();
    }

