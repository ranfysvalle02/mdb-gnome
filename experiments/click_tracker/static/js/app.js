// This runs after the HTML page is fully loaded
document.addEventListener('DOMContentLoaded', () => {
    
    const clickButton = document.getElementById('click-btn');
    const countSpan = document.getElementById('click-count');
    
    // Safety check
    if (!clickButton || !countSpan) {
        console.error("Could not find button or count span!");
        return;
    }

    // Get the API URL we stored in the 'data-url' attribute
    const apiUrl = clickButton.dataset.url;

    // Listen for a click on our button
    clickButton.addEventListener('click', () => {
        
        // Disable the button to prevent double-clicks
        clickButton.disabled = true;
        clickButton.innerText = "Clicking...";

        // Call our new API route /record-click
        fetch(apiUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // IT WORKED! Update the count on the page.
                countSpan.innerText = data.new_count;
            } else {
                console.error("API Error:", data.error);
                countSpan.innerText = "Error!";
            }
        })
        .catch(error => {
            console.error("Fetch Error:", error);
            countSpan.innerText = "Error!";
        })
        .finally(() => {
            // Re-enable the button
            clickButton.disabled = false;
            clickButton.innerText = "Click Me!";
        });
    });
});
