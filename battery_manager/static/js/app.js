/**
 * Kostal Battery Manager - Frontend JavaScript
 */

// Show loading overlay
function showLoading(message = 'Lädt...') {
    const loadingDiv = document.createElement('div');
    loadingDiv.id = 'loading-overlay';
    loadingDiv.className = 'loading';
    loadingDiv.innerHTML = `
        <div style="text-align: center;">
            <div class="spinner"></div>
            <p style="margin-top: 1rem; color: white;">${message}</p>
        </div>
    `;
    document.body.appendChild(loadingDiv);
}

// Hide loading overlay
function hideLoading() {
    const loadingDiv = document.getElementById('loading-overlay');
    if (loadingDiv) {
        loadingDiv.remove();
    }
}

// Show notification
function showNotification(type, message, duration = 3000) {
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.textContent = message;
    document.body.appendChild(notification);
    
    setTimeout(() => {
        notification.style.animation = 'slideOut 0.3s forwards';
        setTimeout(() => notification.remove(), 300);
    }, duration);
}

// Format datetime to German locale
function formatDateTime(dateString) {
    const date = new Date(dateString);
    return date.toLocaleString('de-DE', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

// Format number with German locale
function formatNumber(number, decimals = 2) {
    return number.toLocaleString('de-DE', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
    });
}

// Debounce function for input handlers
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

// Add slideOut animation to CSS dynamically
const style = document.createElement('style');
style.textContent = `
    @keyframes slideOut {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(100%);
            opacity: 0;
        }
    }
`;
document.head.appendChild(style);

// Global error handler
window.addEventListener('error', (event) => {
    console.error('Global error:', event.error);
});

// Handle unhandled promise rejections
window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled promise rejection:', event.reason);
});

console.log('Kostal Battery Manager loaded');
