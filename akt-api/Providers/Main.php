<?php

namespace Modules\AktApi\Providers;

use Illuminate\Cache\RateLimiting\Limit;
use Illuminate\Support\Facades\RateLimiter;
use Illuminate\Support\ServiceProvider;

class Main extends ServiceProvider
{
    /**
     * Register the service provider.
     *
     * @return void
     */
    public function register()
    {
        $this->loadRoutes();
    }

    /**
     * Boot the application events.
     *
     * @return void
     */
    public function boot()
    {
        $this->relaxApiRateLimit();
    }

    /**
     * Exempt the akt-cli admin CLI from Laravel's `throttle:api` limiter.
     *
     * The stock 60/min limit is sized for the browser UI; akt is a trusted
     * admin tool doing bulk data entry (thousands of transactions), where it
     * repeatedly trips the limit and eats retry backoff. Requests presenting
     * the shared secret in the `X-Akt-Bypass` header skip the limit entirely;
     * every other client keeps the default 60/min (keyed by user id, else IP).
     *
     * Module providers boot after the core RouteServiceProvider, so this
     * re-registration of the named 'api' limiter wins. No-op unless
     * AKT_API_BYPASS_TOKEN is set in the environment.
     *
     * @return void
     */
    protected function relaxApiRateLimit()
    {
        $token = (string) env('AKT_API_BYPASS_TOKEN', '');

        RateLimiter::for('api', function ($request) use ($token) {
            $sent = (string) $request->header('X-Akt-Bypass', '');
            if ($token !== '' && hash_equals($token, $sent)) {
                return Limit::none();
            }

            $key = optional($request->user())->id ?: $request->ip();

            return Limit::perMinute(60)->by($key);
        });
    }

    /**
     * Load the module's API routes.
     *
     * @return void
     */
    public function loadRoutes()
    {
        if (app()->routesAreCached()) {
            return;
        }

        $this->loadRoutesFrom(__DIR__ . '/../Routes/api.php');
    }
}
