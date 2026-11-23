\getenv fixture_token GHOSTDEPLOY_FIXTURE_TOKEN
CREATE TABLE public.ghostdeploy_fixture_marker (token text NOT NULL);
INSERT INTO public.ghostdeploy_fixture_marker VALUES (:'fixture_token');
