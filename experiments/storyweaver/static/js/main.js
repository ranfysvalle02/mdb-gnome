// Story Weaver Main JavaScript
// This file will be populated with the full JavaScript functionality
// from the original Flask application

// Copy link functionality
document.addEventListener('DOMContentLoaded', () => {
    // Copy link buttons
    document.querySelectorAll('.copy-link-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const url = btn.dataset.url;
            const fullUrl = window.location.origin + url;
            try {
                await navigator.clipboard.writeText(fullUrl);
                const originalText = btn.textContent;
                btn.textContent = 'Copied!';
                setTimeout(() => {
                    btn.textContent = originalText;
                }, 2000);
            } catch (err) {
                console.error('Failed to copy:', err);
                alert('Failed to copy link. Please copy manually: ' + fullUrl);
            }
        });
    });
});

