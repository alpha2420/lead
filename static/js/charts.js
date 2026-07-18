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
      chartSource = new Chart(document.getElementById('chartSource'), {
        type: 'doughnut',
        data: {
          labels: ['Apollo', 'Apify', 'Explorium'],
          datasets: [{ data: [0, 0, 0], backgroundColor: ['#3b82f6','#22d3ee','#10b981'], borderColor: 'transparent', borderWidth: 2 }]
        },
        options: { ...CHART_DEFAULTS, cutout: '65%' }
      });

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
    }

    function updateCharts(stats) {
      if (chartSource) {
        chartSource.data.datasets[0].data = [stats.apollo || 0, stats.apify || 0, stats.explorium || 0];
        chartSource.update();
      }
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

